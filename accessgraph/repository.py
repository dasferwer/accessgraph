from accessgraph.graph import check


def would_cycle(conn, tenant, resource, subject, relation):
    # UNION, а не UNION ALL, ограничивает обход числом узлов даже при повреждённом графе.
    return conn.execute(
        """WITH RECURSIVE reachable(node) AS (
        SELECT %s::text UNION SELECT e.subject FROM edges e JOIN reachable r ON e.resource=r.node
        WHERE e.tenant=%s AND e.relation=%s
        ) SELECT EXISTS(SELECT 1 FROM reachable WHERE node=%s) AS cycle""",
        (subject, tenant, relation, resource),
    ).fetchone()["cycle"]


def evaluate(conn, tenant, user, resources, permission):
    rows = conn.execute(
        """WITH RECURSIVE ancestors(node) AS (
        SELECT unnest(%s::text[]) UNION SELECT e.subject FROM edges e JOIN ancestors a ON e.resource=a.node
        WHERE e.tenant=%s AND e.relation='parent'),
        principals(node) AS (SELECT %s::text UNION SELECT e.resource FROM edges e JOIN principals p ON e.subject=p.node
        WHERE e.tenant=%s AND e.relation='member')
        SELECT e.resource,e.relation,e.subject FROM edges e
        WHERE e.tenant=%s AND ((e.resource IN (SELECT node FROM ancestors) AND e.relation<>'member')
          OR (e.subject IN (SELECT node FROM principals) AND e.relation='member'))
        ORDER BY e.resource,e.relation,e.subject LIMIT 10001""",
        (resources, tenant, user, tenant, tenant),
    ).fetchall()
    if len(rows) > 10000:
        raise ValueError("Достижимый подграф превышает 10 000 отношений")
    return {resource: check(rows, user, resource, permission) for resource in resources}, len(rows)
