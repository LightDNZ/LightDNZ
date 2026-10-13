import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '')
USER_NAME = os.environ.get('USER_NAME', 'LightDNZ')
HEADERS = {'authorization': f'token {ACCESS_TOKEN}'} if ACCESS_TOKEN else {}
QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0,
}
OWNER_ID = None


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '',
    )


def format_plural(unit):
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        return request
    raise Exception(
        f"{func_name} has failed with status {request.status_code}: {request.text} (query counts: {QUERY_COUNT})"
    )


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        repo = request.json().get('data', {}).get('repository')
        if repo and repo.get('defaultBranchRef') is not None:
            return loc_counter_one_repo(
                owner,
                repo_name,
                data,
                cache_comment,
                repo['defaultBranchRef']['target']['history'],
                addition_total,
                deletion_total,
                my_commits,
            )
        else:
            return addition_total, deletion_total, my_commits
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests! (GitHub Rate limit)')
    raise Exception(f'recursive_loc() failed with status {request.status_code}: {request.text}')


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for node in history.get('edges', []):
        node_data = node.get('node', {})
        author = node_data.get('author')
        if author and author.get('user') == OWNER_ID:
            my_commits += 1
            addition_total += node_data.get('additions', 0)
            deletion_total += node_data.get('deletions', 0)
    if not history.get('edges') or not history.get('pageInfo', {}).get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(
            owner,
            repo_name,
            data,
            cache_comment,
            addition_total,
            deletion_total,
            my_commits,
            history['pageInfo']['endCursor'],
        )


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    data = request.json()['data']['user']['repositories']
    current_edges = edges + data['edges']
    if data['pageInfo']['hasNextPage']:
        return loc_query(owner_affiliation, comment_size, force_cache, data['pageInfo']['endCursor'], current_edges)
    else:
        return cache_builder(current_edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    os.makedirs('cache', exist_ok=True)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('Comment block\n')
        with open(filename, 'w', encoding='utf-8') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        parts = data[index].split()
        repo_hash = parts[0]
        commit_count = parts[1]
        node_branch = edges[index]['node'].get('defaultBranchRef')
        total_branch_commits = node_branch['target']['history']['totalCount'] if node_branch else 0
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != total_branch_commits:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = f"{repo_hash} {total_branch_commits} {loc[2]} {loc[0]} {loc[1]}\n"
            except (TypeError, ValueError, KeyError):
                data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3])
            loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    os.makedirs('cache', exist_ok=True)
    existing_cache = {}
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines[comment_size:]:
                parts = line.split()
                if parts:
                    existing_cache[parts[0]] = line

    comment_lines = []
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            comment_lines = f.readlines()[:comment_size]
    while len(comment_lines) < comment_size:
        comment_lines.append('Comment block\n')

    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(comment_lines)
        for node in edges:
            repo_hash = hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest()
            if repo_hash in existing_cache:
                f.write(existing_cache[repo_hash])
            else:
                f.write(f"{repo_hash} 0 0 0 0\n")


def force_close_file(data, cache_comment):
    os.makedirs('cache', exist_ok=True)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w', encoding='utf-8') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data, 49)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        return 0
    with open(filename, 'r', encoding='utf-8') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


if __name__ == '__main__':
    age_data = daily_readme(datetime.datetime(2008, 8, 8))

    if ACCESS_TOKEN:
        user_data, _ = perf_counter(user_getter, USER_NAME)
        OWNER_ID, _ = user_data
        total_loc, _ = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
        commit_data = commit_counter(7)
        star_data = graph_repos_stars('stars', ['OWNER'])
        repo_data = graph_repos_stars('repos', ['OWNER'])
        contrib_data = graph_repos_stars('repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
        follower_data = follower_getter(USER_NAME)
        formatted_loc = [
            '{:,}'.format(total_loc[0]),
            '{:,}'.format(total_loc[1]),
            '{:,}'.format(total_loc[2]),
        ]
    else:
        print("Notice: ACCESS_TOKEN not detected in environment. Running in local mode using cache.")
        cache_filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
        commit_data = commit_counter(7)
        adds, dels = 0, 0
        repo_count = 0
        if os.path.exists(cache_filename):
            with open(cache_filename, 'r', encoding='utf-8') as f:
                lines = f.readlines()[7:]
                repo_count = len(lines)
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 5:
                        adds += int(parts[3])
                        dels += int(parts[4])
        star_data = 0
        repo_data = repo_count
        contrib_data = repo_count
        follower_data = 0
        formatted_loc = [
            '{:,}'.format(adds),
            '{:,}'.format(dels),
            '{:,}'.format(adds - dels),
        ]

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, formatted_loc)
    print("Successfully updated dark_mode.svg and light_mode.svg!")