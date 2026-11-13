"""Exact integer max-flow helpers for claim-allocation ranges.

The claim problem is a per-currency transportation network::

    source -> receipt   capacity = receipt supply (positive minor units)
    receipt -> group    capacity = unlimited (an allowed relation)
    group -> sink       capacity = positive expected_minor (demand)

Only the Python standard library is used.  Every capacity and flow is a
Python integer; no floating-point arithmetic touches money.  The minimum and
maximum flow of one arc across *all* maximum flows are derived with residual
network queries, never by enumerating minor units or complete allocations.
"""
from collections import deque

INF = 1 << 62


class FlowNetwork:
    """Dinic max flow with explicit forward arcs and retrievable flows.

    Adjacency entries are ``[to, reverse_index, residual_capacity, edge_id,
    sign]``.  ``sign`` is ``+1`` on the forward entry and ``-1`` on its
    reverse, which lets a residual augmentation be translated back into a
    change of original edge flows.
    """

    __slots__ = ("n", "graph", "edges")

    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]
        # edge_id -> (u, v, capacity, forward_index_in_graph[u])
        self.edges = []

    def add_edge(self, u, v, capacity):
        if not isinstance(capacity, int) or capacity < 0:
            raise ValueError("flow capacities must be non-negative integers")
        edge_id = len(self.edges)
        forward_index = len(self.graph[u])
        backward_index = len(self.graph[v])
        self.graph[u].append([v, backward_index, capacity, edge_id, 1])
        self.graph[v].append([u, forward_index, 0, edge_id, -1])
        self.edges.append((u, v, capacity, forward_index))
        return edge_id

    def flow(self, edge_id):
        u, _v, capacity, forward_index = self.edges[edge_id]
        return capacity - self.graph[u][forward_index][2]

    def max_flow(self, source, sink, limit=None):
        """Return the max flow value, stopping early at ``limit`` if given.

        Blocking-flow search is iterative, so networks larger than Python's
        recursion limit are safe.
        """
        if source == sink:
            raise ValueError("source and sink must differ")
        if limit is None:
            limit = INF
        if limit <= 0:
            return 0
        total = 0
        n = self.n
        graph = self.graph
        while total < limit:
            level = [-1] * n
            level[source] = 0
            queue = deque([source])
            while queue:
                u = queue.popleft()
                for entry in graph[u]:
                    if entry[2] > 0 and level[entry[0]] < 0:
                        level[entry[0]] = level[u] + 1
                        queue.append(entry[0])
            if level[sink] < 0:
                break
            pointer = [0] * n
            while total < limit:
                stack = [source]
                path = []
                while stack:
                    u = stack[-1]
                    if u == sink:
                        sent = min(limit - total,
                                   min(entry[2] for entry in path))
                        for entry in path:
                            entry[2] -= sent
                            graph[entry[0]][entry[1]][2] += sent
                        total += sent
                        break
                    advanced = False
                    while pointer[u] < len(graph[u]):
                        entry = graph[u][pointer[u]]
                        if entry[2] > 0 and level[entry[0]] == level[u] + 1:
                            stack.append(entry[0])
                            path.append(entry)
                            advanced = True
                            break
                        pointer[u] += 1
                    if not advanced:
                        level[u] = -1  # dead end for the rest of this phase
                        stack.pop()
                        if path:
                            path.pop()
                else:
                    break
        return total

    def reachable(self, source):
        """Nodes reachable from source in the current residual network."""
        seen = {source}
        queue = deque([source])
        while queue:
            u = queue.popleft()
            for entry in self.graph[u]:
                if entry[2] > 0 and entry[0] not in seen:
                    seen.add(entry[0])
                    queue.append(entry[0])
        return seen


def residual_copy(network, exclude_edge=None):
    """Copy the current residual network, optionally removing one edge.

    Returns ``(copy, metadata)`` where metadata maps each copied forward arc
    to ``(original_edge_id, sign)``.  Both directions of ``exclude_edge`` are
    omitted, so a return path cannot simply cancel the arc being examined.
    """
    copy = FlowNetwork(network.n)
    metadata = []
    for u in range(network.n):
        for entry in network.graph[u]:
            v, _reverse, capacity, edge_id, sign = entry
            if capacity <= 0 or edge_id == exclude_edge:
                continue
            copied = copy.add_edge(u, v, capacity)
            metadata.append((copied, edge_id, sign))
    return copy, metadata


def adjusted_flows(base_flows, residual_network, metadata, edge_id, delta):
    """Apply a residual circulation plus ``delta`` on ``edge_id``.

    ``delta`` is positive for a forward increase and negative for a forward
    decrease.  The residual flow is integral, so the result stays integral.
    """
    result = list(base_flows)
    for copied, original, sign in metadata:
        amount = residual_network.flow(copied)
        if amount:
            result[original] += sign * amount
    result[edge_id] += delta
    return result


def edge_flow_range(network, base_flows, edge_id, source, sink):
    """Minimum and maximum flow on one arc among all maximum flows.

    Returns ``(minimum, maximum, min_allocation, max_allocation)``.  The
    allocations are complete integral flow vectors for the whole network and
    both have the same total value as ``base_flows``.
    """
    u, v, capacity, _forward = network.edges[edge_id]
    flow = base_flows[edge_id]

    # Increase: push delta over the arc forward, then return v -> u through
    # the residual network without using either direction of the arc.
    maximum = flow
    max_allocation = base_flows
    forward_residual = capacity - flow
    if forward_residual > 0:
        residual, metadata = residual_copy(network, exclude_edge=edge_id)
        delta = residual.max_flow(v, u, limit=forward_residual)
        if delta:
            maximum = flow + delta
            max_allocation = adjusted_flows(
                base_flows, residual, metadata, edge_id, delta)

    # Decrease: cancel delta of the arc, then route delta back u -> v.
    minimum = flow
    min_allocation = base_flows
    if flow > 0:
        residual, metadata = residual_copy(network, exclude_edge=edge_id)
        delta = residual.max_flow(u, v, limit=flow)
        if delta:
            minimum = flow - delta
            min_allocation = adjusted_flows(
                base_flows, residual, metadata, edge_id, -delta)

    return minimum, maximum, min_allocation, max_allocation
