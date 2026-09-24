from accessgraph.graph import check, creates_cycle


def edge(resource, relation, subject):
    return {"resource": resource, "relation": relation, "subject": subject}


def test_nested_team_rights_and_unknown_user():
    graph = [
        edge("team:t", "member", "user:a"),
        edge("folder:root", "editor", "team:t"),
        edge("folder:sub", "parent", "folder:root"),
        edge("document:d", "parent", "folder:sub"),
    ]
    result = check(graph, "user:a", "document:d")
    assert result["allowed"]
    assert result["path"] == ["document:d", "folder:sub", "folder:root", "editor", "team:t"]
    assert not check(graph, "user:b", "document:d")["allowed"]


def test_cycles_terminate_and_are_detected():
    graph = [edge("folder:a", "parent", "folder:b"), edge("folder:b", "parent", "folder:c")]
    assert creates_cycle(graph, "folder:c", "folder:a")
    assert creates_cycle([], "folder:a", "folder:a")
    assert not creates_cycle(graph, "folder:d", "folder:a")
    graph.append(edge("folder:c", "parent", "folder:a"))
    assert not check(graph, "user:x", "folder:a")["allowed"]


def test_right_does_not_flow_upward():
    graph = [edge("document:d", "owner", "user:a"), edge("document:d", "parent", "folder:f")]
    assert not check(graph, "user:a", "folder:f")["allowed"]
