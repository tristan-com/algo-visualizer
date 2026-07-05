"""
Algorithm Visualizer - Backend

Two endpoints:
  POST /solve      — original batch endpoint (returns all steps at once)
  WS   /ws/solve   — websocket endpoint (streams each step live as it happens)

The algorithms are now generators that yield steps as they run.
The batch endpoint collects all yielded steps into lists.
The websocket endpoint sends each step over the socket as it's yielded.
"""

import asyncio
import json
from collections import deque
import heapq

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Algorithm Visualizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic models (unchanged — used by /solve REST endpoint)
# ---------------------------------------------------------------------------

class SolveRequest(BaseModel):
    rows: int
    cols: int
    start: list[int]
    end: list[int]
    walls: list[list[int]]
    algorithm: str
    weights: dict[str, int] | None = None


class SolveResponse(BaseModel):
    visited_order: list[list[int]]
    path: list[list[int]]
    found: bool


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def neighbors(r, c, rows, cols):
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            yield nr, nc


def reconstruct_path(came_from, start, end):
    if end not in came_from and end != start:
        return []
    path = [end]
    cur = end
    while cur != start:
        cur = came_from[cur]
        path.append(cur)
    path.reverse()
    return [list(p) for p in path]


# ---------------------------------------------------------------------------
# Algorithms — now generators
#
# Each algorithm yields dicts as it runs:
#   {"type": "visited", "cell": [r, c]}  — a cell was just expanded
#   {"type": "path",    "cell": [r, c]}  — a cell is on the final path
#   {"type": "done",    "found": bool}   — algorithm finished
#
# This means the same generator works for both the REST endpoint
# (collect everything into lists) and the websocket endpoint (stream live).
# ---------------------------------------------------------------------------

def gen_bfs(rows, cols, start, end, wall_set):
    start, end = tuple(start), tuple(end)
    visited = {start}
    came_from = {}
    queue = deque([start])

    while queue:
        current = queue.popleft()
        yield {"type": "visited", "cell": list(current)}

        if current == end:
            path = reconstruct_path(came_from, start, end)
            for cell in path:
                yield {"type": "path", "cell": cell}
            yield {"type": "done", "found": True}
            return

        for n in neighbors(*current, rows, cols):
            if n not in visited and n not in wall_set:
                visited.add(n)
                came_from[n] = current
                queue.append(n)

    yield {"type": "done", "found": False}


def gen_dfs(rows, cols, start, end, wall_set):
    start, end = tuple(start), tuple(end)
    visited = set()
    came_from = {}
    stack = [start]

    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        yield {"type": "visited", "cell": list(current)}

        if current == end:
            path = reconstruct_path(came_from, start, end)
            for cell in path:
                yield {"type": "path", "cell": cell}
            yield {"type": "done", "found": True}
            return

        for n in neighbors(*current, rows, cols):
            if n not in visited and n not in wall_set:
                came_from[n] = current
                stack.append(n)

    yield {"type": "done", "found": False}


def gen_dijkstra(rows, cols, start, end, wall_set, weights):
    start, end = tuple(start), tuple(end)
    visited = set()
    came_from = {}
    dist = {start: 0}
    pq = [(0, start)]

    while pq:
        d, current = heapq.heappop(pq)
        if current in visited:
            continue
        visited.add(current)
        yield {"type": "visited", "cell": list(current)}

        if current == end:
            path = reconstruct_path(came_from, start, end)
            for cell in path:
                yield {"type": "path", "cell": cell}
            yield {"type": "done", "found": True}
            return

        for n in neighbors(*current, rows, cols):
            if n in wall_set or n in visited:
                continue
            cost = weights.get(f"{n[0]},{n[1]}", 1)
            new_dist = d + cost
            if n not in dist or new_dist < dist[n]:
                dist[n] = new_dist
                came_from[n] = current
                heapq.heappush(pq, (new_dist, n))

    yield {"type": "done", "found": False}


def heuristic(a, b):
    """Manhattan distance — admissible heuristic for a 4-directional grid."""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def gen_astar(rows, cols, start, end, wall_set, weights):
    start, end = tuple(start), tuple(end)
    visited = set()
    came_from = {}
    g = {start: 0}
    pq = [(heuristic(start, end), start)]

    while pq:
        f, current = heapq.heappop(pq)
        if current in visited:
            continue
        visited.add(current)
        yield {"type": "visited", "cell": list(current)}

        if current == end:
            path = reconstruct_path(came_from, start, end)
            for cell in path:
                yield {"type": "path", "cell": cell}
            yield {"type": "done", "found": True}
            return

        for n in neighbors(*current, rows, cols):
            if n in wall_set or n in visited:
                continue
            cost = weights.get(f"{n[0]},{n[1]}", 1)
            tentative_g = g[current] + cost
            if n not in g or tentative_g < g[n]:
                g[n] = tentative_g
                came_from[n] = current
                heapq.heappush(pq, (tentative_g + heuristic(n, end), n))

    yield {"type": "done", "found": False}


def get_generator(algorithm, rows, cols, start, end, wall_set, weights):
    weights = weights or {}
    match algorithm:
        case "bfs":      return gen_bfs(rows, cols, start, end, wall_set)
        case "dfs":      return gen_dfs(rows, cols, start, end, wall_set)
        case "dijkstra": return gen_dijkstra(rows, cols, start, end, wall_set, weights)
        case "astar":    return gen_astar(rows, cols, start, end, wall_set, weights)
        case _:          return None


# ---------------------------------------------------------------------------
# REST endpoint — unchanged behaviour, now built on the generators
# ---------------------------------------------------------------------------

@app.post("/solve", response_model=SolveResponse)
def solve(req: SolveRequest):
    wall_set = {tuple(w) for w in req.walls}
    gen = get_generator(req.algorithm, req.rows, req.cols,
                        req.start, req.end, wall_set, req.weights)
    if gen is None:
        return SolveResponse(visited_order=[], path=[], found=False)

    visited_order, path, found = [], [], False
    for step in gen:
        if step["type"] == "visited":
            visited_order.append(step["cell"])
        elif step["type"] == "path":
            path.append(step["cell"])
        elif step["type"] == "done":
            found = step["found"]

    return SolveResponse(visited_order=visited_order, path=path, found=found)


# ---------------------------------------------------------------------------
# WebSocket endpoint — streams each step live
#
# Protocol:
#   CLIENT sends one JSON message with the grid params (same shape as SolveRequest)
#   SERVER streams messages: {"type": "visited"|"path"|"done", ...}
#   SERVER closes the connection after "done"
#
# The `delay` field in the client message controls how long the backend sleeps
# between steps (in seconds). Frontend sends e.g. 0.02 for "fast".
# ---------------------------------------------------------------------------

@app.websocket("/ws/solve")
async def ws_solve(websocket: WebSocket):
    await websocket.accept()

    try:
        # Wait for the client to send grid params
        raw = await websocket.receive_text()
        data = json.loads(raw)

        rows      = data["rows"]
        cols      = data["cols"]
        start     = data["start"]
        end       = data["end"]
        wall_set  = {tuple(w) for w in data["walls"]}
        algorithm = data["algorithm"]
        weights   = data.get("weights") or {}
        delay     = float(data.get("delay", 0.02))  # seconds between steps

        gen = get_generator(algorithm, rows, cols, start, end, wall_set, weights)
        if gen is None:
            await websocket.send_text(json.dumps({"type": "done", "found": False}))
            return

        for step in gen:
            await websocket.send_text(json.dumps(step))
            # Yield control to the event loop so FastAPI can actually send
            # the message before continuing to the next step.
            await asyncio.sleep(delay)

    except WebSocketDisconnect:
        # Client closed the tab or hit Reset mid-animation — that's fine.
        pass

    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Maze generation — generators that yield wall/passage steps
#
# Both generators yield:
#   {"type": "wall",    "cell": [r, c]}  — set this cell to wall
#   {"type": "passage", "cell": [r, c]}  — set this cell to empty/passage
#   {"type": "done"}                     — maze complete
#
# The frontend updates both the visual class AND its grid[][] state array
# on each message, so the maze is immediately ready for pathfinding.
# ---------------------------------------------------------------------------

import random


def gen_maze_dfs(rows, cols, start, end):
    """
    Randomized DFS (recursive backtracker).

    Works on a 2x-scaled "room grid": maze rooms live at even coordinates,
    wall cells live between them at odd coordinates.

    Steps:
      1. Fill entire grid with walls.
      2. Pick a starting room and carve from there.
      3. DFS: pick a random unvisited neighbour room, remove the wall between
         them (the cell at the midpoint), mark both as passages, recurse.
      4. Backtrack when stuck — this guarantees every room is reachable
         and the maze has exactly one solution between any two points.

    Produces long, winding corridors with a single guaranteed path.
    """
    start, end = tuple(start), tuple(end)

    # Step 1: fill everything with walls
    for r in range(rows):
        for c in range(cols):
            yield {"type": "wall", "cell": [r, c]}

    # Rooms live at even (r, c) positions
    # Clamp to the even grid that fits inside our rows/cols
    room_rows = rows // 2
    room_cols = cols // 2

    visited = set()

    def room_to_cell(rr, rc):
        return (rr * 2, rc * 2)

    def carve(rr, rc):
        visited.add((rr, rc))
        cr, cc = room_to_cell(rr, rc)
        yield {"type": "passage", "cell": [cr, cc]}

        # Shuffle neighbour directions for randomness
        directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        random.shuffle(directions)

        for dr, dc in directions:
            nr, nc = rr + dr, rc + dc
            if 0 <= nr < room_rows and 0 <= nc < room_cols and (nr, nc) not in visited:
                # Carve the wall between current room and neighbour
                wall_r, wall_c = cr + dr, cc + dc
                yield {"type": "passage", "cell": [wall_r, wall_c]}
                yield from carve(nr, nc)

    # Start carving from room (0, 0)
    yield from carve(0, 0)

    # Make sure start and end cells are passages (they may be on odd coords)
    yield {"type": "passage", "cell": list(start)}
    yield {"type": "passage", "cell": list(end)}

    # Carve a small clear zone around start and end so they're always reachable
    for dr, dc in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
        sr, sc = start[0]+dr, start[1]+dc
        er, ec = end[0]+dr, end[1]+dc
        if 0 <= sr < rows and 0 <= sc < cols:
            yield {"type": "passage", "cell": [sr, sc]}
        if 0 <= er < rows and 0 <= ec < cols:
            yield {"type": "passage", "cell": [er, ec]}

    yield {"type": "done"}


def gen_maze_division(rows, cols, start, end):
    """
    Recursive Division.

    Starts with an open grid, draws border walls, then recursively splits
    chambers by inserting a wall with one gap.

    r1,c1,r2,c2 are the WALL BOUNDARY cells of the chamber.
    Walls draw through interior only (r1+1..r2-1 / c1+1..c2-1).
    Sub-chambers share the newly drawn wall as their boundary.

    Post-processes connectivity: if start cannot reach end after generation
    (can happen when two adjacent walls have gaps at different positions),
    carves a minimal L-shaped corridor to guarantee a solution.
    """
    start, end = tuple(start), tuple(end)

    # Track final grid state for the connectivity check at the end.
    # We build it in parallel as we yield steps.
    final = [["passage"] * cols for _ in range(rows)]

    def emit(type_, r, c):
        final[r][c] = type_
        return {"type": type_, "cell": [r, c]}

    # Step 1: clear everything
    for r in range(rows):
        for c in range(cols):
            yield emit("passage", r, c)

    # Step 2: border walls
    for c in range(cols):
        yield emit("wall", 0, c)
        yield emit("wall", rows - 1, c)
    for r in range(1, rows - 1):
        yield emit("wall", r, 0)
        yield emit("wall", r, cols - 1)

    def divide(r1, c1, r2, c2):
        interior_h = r2 - r1 - 1
        interior_w = c2 - c1 - 1

        if interior_h < 2 or interior_w < 2:
            return

        if interior_h > interior_w:
            horizontal = True
        elif interior_w > interior_h:
            horizontal = False
        else:
            horizontal = random.random() < 0.5

        if horizontal:
            wall_row = random.randint(r1 + 1, r2 - 1)
            gap_col  = random.randint(c1 + 1, c2 - 1)
            # Protect start/end if they sit exactly on this wall
            for p in [start, end]:
                if p[0] == wall_row and c1 < p[1] < c2:
                    gap_col = p[1]
            for c in range(c1 + 1, c2):
                if c != gap_col:
                    yield emit("wall", wall_row, c)
            yield from divide(r1, c1, wall_row, c2)
            yield from divide(wall_row, c1, r2, c2)
        else:
            wall_col = random.randint(c1 + 1, c2 - 1)
            gap_row  = random.randint(r1 + 1, r2 - 1)
            for p in [start, end]:
                if p[1] == wall_col and r1 < p[0] < r2:
                    gap_row = p[0]
            for r in range(r1 + 1, r2):
                if r != gap_row:
                    yield emit("wall", r, wall_col)
            yield from divide(r1, c1, r2, wall_col)
            yield from divide(r1, wall_col, r2, c2)

    yield from divide(0, 0, rows - 1, cols - 1)

    # Ensure start and end cells are passages
    yield emit("passage", start[0], start[1])
    yield emit("passage", end[0], end[1])

    # ---- Connectivity check & corridor fix ----
    # BFS from start to see if end is reachable.
    from collections import deque

    def bfs_reach(sr, sc):
        q = deque([(sr, sc)])
        seen = {(sr, sc)}
        while q:
            r, c = q.popleft()
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in seen and final[nr][nc] != "wall":
                    seen.add((nr, nc))
                    q.append((nr, nc))
        return seen

    reachable = bfs_reach(*start)

    if tuple(end) not in reachable:
        # Find the reachable cell closest (Manhattan) to end
        er, ec = end
        closest = min(reachable, key=lambda p: abs(p[0] - er) + abs(p[1] - ec))

        # Carve an L-shaped corridor: first go along rows, then columns
        # (or the other way if that's shorter — try both and pick fewer walls cleared)
        def corridor_cells_h_then_v(fr, fc, tr, tc):
            """Horizontal first, then vertical."""
            cells = []
            c = fc
            step = 1 if tc > fc else -1
            while c != tc:
                c += step
                cells.append((fr, c))
            r = fr
            step = 1 if tr > fr else -1
            while r != tr:
                r += step
                cells.append((r, tc))
            return cells

        def corridor_cells_v_then_h(fr, fc, tr, tc):
            """Vertical first, then horizontal."""
            cells = []
            r = fr
            step = 1 if tr > fr else -1
            while r != tr:
                r += step
                cells.append((r, fc))
            c = fc
            step = 1 if tc > fc else -1
            while c != tc:
                c += step
                cells.append((tr, c))
            return cells

        cr, cc = closest
        route_a = corridor_cells_h_then_v(cr, cc, er, ec)
        route_b = corridor_cells_v_then_h(cr, cc, er, ec)

        # Pick whichever route clears fewer walls
        walls_a = sum(1 for r, c in route_a if final[r][c] == "wall")
        walls_b = sum(1 for r, c in route_b if final[r][c] == "wall")
        route = route_a if walls_a <= walls_b else route_b

        for r, c in route:
            if final[r][c] == "wall" and [r, c] != list(start) and [r, c] != list(end):
                yield emit("passage", r, c)

    yield {"type": "done"}


MAZE_GENERATORS = {
    "dfs":      gen_maze_dfs,
    "division": gen_maze_division,
}


@app.websocket("/ws/maze")
async def ws_maze(websocket: WebSocket):
    await websocket.accept()

    try:
        raw  = await websocket.receive_text()
        data = json.loads(raw)

        rows      = data["rows"]
        cols      = data["cols"]
        start     = data["start"]
        end       = data["end"]
        algorithm = data.get("algorithm", "dfs")
        delay     = float(data.get("delay", 0.005))

        gen_fn = MAZE_GENERATORS.get(algorithm)
        if gen_fn is None:
            await websocket.send_text(json.dumps({"type": "done"}))
            return

        for step in gen_fn(rows, cols, start, end):
            await websocket.send_text(json.dumps(step))
            await asyncio.sleep(delay)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass


@app.get("/")
def root():
    return {"status": "ok", "message": "Algorithm Visualizer API. POST /solve or WS /ws/solve or /ws/maze."}