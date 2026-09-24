from collections import defaultdict, deque


def check(edges, user, resource, permission="view"):
    rights = defaultdict(list)
    parents = defaultdict(list)
    teams = set()
    membership = defaultdict(set)
    for edge in edges:
        if edge["relation"] == "member":
            membership[edge["subject"]].add(edge["resource"])
    pending = [user]
    while pending:
        for team in membership[pending.pop()]:
            if team not in teams:
                teams.add(team)
                pending.append(team)
    allowed = {
        "view": {"owner", "editor", "viewer"},
        "edit": {"owner", "editor"},
        "manage": {"owner"},
    }[permission]
    for edge in edges:
        if edge["relation"] == "member" and edge["subject"] == user:
            teams.add(edge["resource"])
        elif edge["relation"] == "parent":
            parents[edge["resource"]].append(edge["subject"])
        elif edge["relation"] in allowed:
            rights[edge["resource"]].append(edge)
    queue = deque([(resource, [resource])])
    visited = set()
    while queue:
        node, path = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        for edge in rights[node]:
            if edge["subject"] == user or edge["subject"] in teams:
                return {"allowed": True, "path": path + [edge["relation"], edge["subject"]]}
        queue.extend((parent, path + [parent]) for parent in parents[node])
    return {"allowed": False, "path": []}


def creates_cycle(edges, child, parent):
    parents = defaultdict(list)
    for edge in edges:
        if edge["relation"] == "parent":
            parents[edge["resource"]].append(edge["subject"])
    pending, visited = [parent], set()
    while pending:
        node = pending.pop()
        if node == child:
            return True
        if node not in visited:
            visited.add(node)
            pending.extend(parents[node])
    return False
