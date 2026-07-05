// ---- Config ----
const ROWS = 20;
const COLS = 35;
const WS_SOLVE_URL = "ws://localhost:8000/ws/solve";
const WS_MAZE_URL  = "ws://localhost:8000/ws/maze";
const REST_URL     = "http://localhost:8000/solve";
const MUD_COST     = 5;

const SPEED_DELAYS = { "15": 0.012, "40": 0.03, "80": 0.07 };

// ---- State ----
let grid      = [];       // grid[r][c] = "empty" | "wall" | "mud"
let startCell = [2, 2];
let endCell   = [ROWS - 3, COLS - 3];
let isMouseDown  = false;
let mouseAction  = null;
let drawMode     = "wall";
let isRunning    = false;
let activeSocket = null;

// ---- DOM ----
const container    = document.getElementById("grid-container");
const statusEl     = document.getElementById("status");
const visualizeBtn = document.getElementById("visualize-btn");
const stopBtn      = document.getElementById("stop-btn");

container.style.gridTemplateColumns = `repeat(${COLS}, 24px)`;
container.style.gridTemplateRows    = `repeat(${ROWS}, 24px)`;

// ---- Grid helpers ----
function buildGrid() {
  container.innerHTML = "";
  grid = [];
  for (let r = 0; r < ROWS; r++) {
    const row = [];
    for (let c = 0; c < COLS; c++) {
      row.push("empty");
      const cell = document.createElement("div");
      cell.className = "cell";
      cell.dataset.row = r;
      cell.dataset.col = c;
      container.appendChild(cell);
    }
    grid.push(row);
  }
  paintEndpoints();
}

function cellEl(r, c) {
  return container.children[r * COLS + c];
}

function paintEndpoints() {
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++)
      cellEl(r, c).classList.remove("start", "end");
  cellEl(...startCell).classList.add("start");
  cellEl(...endCell).classList.add("end");
}

function clearVisualization() {
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++)
      cellEl(r, c).classList.remove("visited", "path");
}

function clearWalls() {
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++) {
      grid[r][c] = "empty";
      cellEl(r, c).classList.remove("wall", "mud");
    }
  clearVisualization();
}

function resetGrid() {
  cancelRun();
  startCell = [2, 2];
  endCell   = [ROWS - 3, COLS - 3];
  buildGrid();
}

// ---- Draw mode ----
function setDrawMode(mode) {
  drawMode = mode;
  document.getElementById("wall-btn").classList.toggle("active", mode === "wall");
  document.getElementById("mud-btn").classList.toggle("active",  mode === "mud");
}

// ---- Mouse interaction ----
function isStart(r, c) { return r === startCell[0] && c === startCell[1]; }
function isEnd(r, c)   { return r === endCell[0]   && c === endCell[1];   }

function applyCell(r, c, el) {
  if (isStart(r, c) || isEnd(r, c)) return;
  if (grid[r][c] === drawMode) {
    grid[r][c] = "empty";
    el.classList.remove("wall", "mud");
    mouseAction = "erase";
  } else {
    grid[r][c] = drawMode;
    el.classList.remove("wall", "mud");
    el.classList.add(drawMode);
    mouseAction = drawMode;
  }
}

container.addEventListener("mousedown", (e) => {
  if (isRunning) return;
  const target = e.target.closest(".cell");
  if (!target) return;
  const r = +target.dataset.row, c = +target.dataset.col;
  isMouseDown = true;
  if      (isStart(r, c)) mouseAction = "drag-start";
  else if (isEnd(r, c))   mouseAction = "drag-end";
  else                    applyCell(r, c, target);
});

container.addEventListener("mouseover", (e) => {
  if (!isMouseDown || isRunning) return;
  const target = e.target.closest(".cell");
  if (!target) return;
  const r = +target.dataset.row, c = +target.dataset.col;

  if ((mouseAction === "wall" || mouseAction === "mud") && !isStart(r,c) && !isEnd(r,c)) {
    grid[r][c] = mouseAction;
    target.classList.remove("wall", "mud");
    target.classList.add(mouseAction);
  } else if (mouseAction === "erase") {
    grid[r][c] = "empty";
    target.classList.remove("wall", "mud");
  } else if (mouseAction === "drag-start" && !isEnd(r, c)) {
    startCell = [r, c];
    paintEndpoints();
  } else if (mouseAction === "drag-end" && !isStart(r, c)) {
    endCell = [r, c];
    paintEndpoints();
  }
});

document.addEventListener("mouseup", () => { isMouseDown = false; mouseAction = null; });
container.addEventListener("dragstart", (e) => e.preventDefault());

// ---- Apply a single maze step to grid state + DOM ----
// Called once per websocket message during maze generation.
function applyMazeStep(type, r, c) {
  if (isStart(r, c) || isEnd(r, c)) return;  // never overwrite start/end

  if (type === "wall") {
    grid[r][c] = "wall";
    const el = cellEl(r, c);
    el.classList.remove("empty", "mud", "visited", "path");
    el.classList.add("wall");
  } else if (type === "passage") {
    grid[r][c] = "empty";
    const el = cellEl(r, c);
    el.classList.remove("wall", "mud", "visited", "path");
  }
}

// ---- Weights ----
function buildWeights() {
  const w = {};
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++)
      if (grid[r][c] === "mud") w[`${r},${c}`] = MUD_COST;
  return w;
}

// ---- Cancel an in-progress run ----
function cancelRun() {
  if (activeSocket) {
    activeSocket.close();
    activeSocket = null;
  }
  setRunning(false);
}

function setRunning(val) {
  isRunning = val;
  visualizeBtn.disabled = val;
  document.getElementById("maze-btn").disabled = val;
  stopBtn.style.display = val ? "inline-block" : "none";
}

// ---- Maze generation ----
function generateMaze() {
  if (isRunning) return;
  clearVisualization();
  clearWalls();       // start fresh — maze fills the grid from scratch
  setRunning(true);
  statusEl.textContent = "Generating maze...";

  const algorithm = document.getElementById("maze-algo").value;
  // Maze builds fast, so use a short delay to make it visible
  const delay = 0.003;

  const payload = JSON.stringify({
    rows: ROWS, cols: COLS,
    start: startCell, end: endCell,
    algorithm, delay,
  });

  let ws;
  try { ws = new WebSocket(WS_MAZE_URL); }
  catch (err) {
    statusEl.textContent = "WebSocket not supported.";
    setRunning(false);
    return;
  }
  activeSocket = ws;

  ws.onopen  = () => ws.send(payload);

  ws.onmessage = (event) => {
    const step = JSON.parse(event.data);
    if (step.type === "wall" || step.type === "passage") {
      applyMazeStep(step.type, step.cell[0], step.cell[1]);
    } else if (step.type === "done") {
      paintEndpoints();   // re-stamp start/end on top of the maze
      statusEl.textContent = "Maze ready — now run a pathfinding algorithm!";
      ws.close();
    } else if (step.type === "error") {
      statusEl.textContent = `Error: ${step.message}`;
      ws.close();
    }
  };

  ws.onerror = () => {
    statusEl.textContent = "Could not reach backend on :8000";
    setRunning(false);
  };

  ws.onclose = () => {
    activeSocket = null;
    setRunning(false);
  };
}

// ---- Pathfinding visualize ----
function visualize() {
  if (isRunning) return;
  clearVisualization();
  setRunning(true);
  statusEl.textContent = "Solving...";

  const walls = [];
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++)
      if (grid[r][c] === "wall") walls.push([r, c]);

  const algorithm = document.getElementById("algorithm").value;
  const speedKey  = document.getElementById("speed").value;
  const delay     = SPEED_DELAYS[speedKey] ?? 0.02;
  const weights   = buildWeights();

  const payload = JSON.stringify({
    rows: ROWS, cols: COLS,
    start: startCell, end: endCell,
    walls, algorithm, weights, delay,
  });

  let pathCells  = [];
  let visitCount = 0;
  let ws;

  try { ws = new WebSocket(WS_SOLVE_URL); }
  catch (err) { fallbackRest(payload, speedKey); return; }
  activeSocket = ws;

  ws.onopen    = () => { statusEl.textContent = "Solving..."; ws.send(payload); };

  ws.onmessage = (event) => {
    const step = JSON.parse(event.data);
    if (step.type === "visited") {
      const [r, c] = step.cell;
      if (!isStart(r, c) && !isEnd(r, c))
        cellEl(r, c).classList.add("visited");
      visitCount++;
    } else if (step.type === "path") {
      pathCells.push(step.cell);
    } else if (step.type === "done") {
      for (const [r, c] of pathCells)
        if (!isStart(r, c) && !isEnd(r, c))
          cellEl(r, c).classList.add("path");

      if (step.found) {
        const mudInPath = pathCells.filter(([r,c]) => grid[r][c] === "mud").length;
        const cost = (pathCells.length - 1) + mudInPath * (MUD_COST - 1);
        statusEl.textContent = `Path found — cost: ${cost}, ${visitCount} cells visited.`;
      } else {
        statusEl.textContent = `No path found. ${visitCount} cells visited.`;
      }
      ws.close();
    } else if (step.type === "error") {
      statusEl.textContent = `Error: ${step.message}`;
      ws.close();
    }
  };

  ws.onerror = () => { fallbackRest(payload, speedKey); };
  ws.onclose = () => { activeSocket = null; setRunning(false); };
}

// ---- REST fallback ----
async function fallbackRest(payload, speedKey) {
  const delay = parseInt(speedKey);
  let data;
  try {
    const res = await fetch(REST_URL, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: payload,
    });
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Error: backend unreachable on :8000";
    setRunning(false);
    return;
  }
  for (const [r,c] of data.visited_order) {
    if (!isStart(r,c) && !isEnd(r,c)) cellEl(r,c).classList.add("visited");
    await new Promise(res => setTimeout(res, delay));
  }
  for (const [r,c] of data.path) {
    if (!isStart(r,c) && !isEnd(r,c)) cellEl(r,c).classList.add("path");
    await new Promise(res => setTimeout(res, delay * 2));
  }
  statusEl.textContent = data.found
    ? `Path found — ${data.path.length - 1} steps, ${data.visited_order.length} cells visited.`
    : `No path found. ${data.visited_order.length} cells visited.`;
  setRunning(false);
}

// ---- Wire up buttons ----
visualizeBtn.addEventListener("click", visualize);
document.getElementById("maze-btn").addEventListener("click", generateMaze);
stopBtn.addEventListener("click", () => {
  cancelRun();
  clearVisualization();
  statusEl.textContent = "Stopped.";
});
document.getElementById("wall-btn").addEventListener("click", () => setDrawMode("wall"));
document.getElementById("mud-btn").addEventListener("click",  () => setDrawMode("mud"));
document.getElementById("clear-walls-btn").addEventListener("click", () => { if (!isRunning) clearWalls(); });
document.getElementById("reset-btn").addEventListener("click", resetGrid);

// ---- Init ----
buildGrid();
setDrawMode("wall");