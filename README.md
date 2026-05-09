# XNAV — AI Spatial Code Navigator

---

## The Problem

Every developer knows the feeling. You open a new repo, or your Vibe-coded project. There are 200 files, 40 folders, and zero context. You start reading `index.js`, then `utils.js`, then something called `handler_v2_final.py`. An hour passes. You've barely scratched the surface.

The average developer spends ~4 hours building a mental map of an unfamiliar codebase before they can contribute meaningfully.

XNAV exists to fix that.

---

## What is XNAV?

XNAV is an AI-powered spatial code navigator. You point it at any codebase, and within minutes it produces a live, interactive graph of your project's architecture — not just a file tree, but a semantic map of how your code actually thinks.

It clusters files into meaningful groups (Backend, Auth, API, Frontend, Config…), draws the dependency flow between them, and lets you drill into any cluster to see individual files, functions, and classes — all explained in plain English by an AI.

---

## What It Solves

There are three moments where developers waste the most time on an unfamiliar codebase:

| Question | Traditional tools |
|---|---|
| "Where does X live?" | File tree — no semantics |
| "What touches Y?" | Text search — no context |
| "How does this fit together?" | Docs — usually stale |

XNAV attacks all three simultaneously, spatially.

---

## How It Works

**1. Parse**
XNAV walks the repo and builds a dependency graph using AST analysis — imports, function calls, and class inheritance across Python, JavaScript, TypeScript, and more.

**2. Embed**
Every file gets a semantic embedding using `sentence-transformers` running on GPU (AMD ROCm in production). Files that do similar things end up close together in embedding space.

**3. Cluster**
An LLM (Mistral via Ollama) reads the graph structure and embeddings and assigns meaningful cluster names — not generic labels, but names like "Auth", "API Gateway", "Data Models". A path-heuristic fallback ensures nothing falls through the cracks.

**4. Explain**
Click any node, and a second LLM call generates a 1–2 sentence explanation of what that file or function does and why it exists — plus dependency context and risk observations.

The result renders as an animated D3.js force-directed spatial graph. Clusters are live: dots drift inside them, and dependency arrows flow between clusters in real time showing which modules import from which.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python + FastAPI |
| Graph engine | NetworkX |
| Embeddings | `sentence-transformers` + AMD GPU (ROCm/PyTorch) |
| LLM | Mistral via Ollama — local, no API cost |
| Frontend | D3.js + vanilla JS |
| Infra | AMD Developer Cloud GPU Droplet |

Running the LLM and embeddings locally means **no API keys required** and **no data leaves your machine** for the default setup.

---

## What's Next

A full blown spatial IDE that scales with the rise of AI and the size of generated code — which is getting harder to read in traditional IDEs.

---

## Setup

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally
- Mistral model pulled: `ollama pull mistral`

### Install

```bash
git clone https://github.com/hasseneafif/xnav
cd xnav
pip install -r requirements.txt
```

### Run

```bash
python cli.py init /path/to/your/project
```

Then open `http://localhost:8000` in your browser.

### GPU / AMD ROCm (optional)

XNAV uses `sentence-transformers` for embeddings. If you have an AMD GPU with ROCm, it will automatically use it. For CPU-only, it still works — just slower on large repos.

For deployment on AMD Developer Cloud, use the provided `Dockerfile`:

```bash
docker build -t xnav .
docker run -p 8000:8000 xnav
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `XNAV_LLM_URL` | `http://localhost:11434` | Ollama or OpenAI-compatible endpoint |
| `XNAV_LLM_MODEL` | `mistral` | Model name |
| `XNAV_LLM_KEY` | _(empty)_ | API key (only needed for cloud providers) |

---

**GitHub:** [github.com/hasseneafif/xnav](https://github.com/hasseneafif/xnav)
