---
name: networkx
description: Creates, analyzes, and visualizes complex networks and graphs in Python with NetworkX. Use when working with network/graph data structures, computing graph algorithms (shortest paths, centrality, clustering), detecting communities, generating synthetic networks (random, scale-free, small-world), reading/writing graph file formats, or drawing network topologies. Common applications include social, biological, transportation, and citation networks.
license: 3-clause BSD license
compatibility: Requires Python >=3.12 (excluding 3.14.1) and NetworkX 3.7. NumPy, SciPy, pandas and Matplotlib support numerical, tabular and drawing examples; optional integrations need their own packages. No credentials; network needed only for installation or remote data.
metadata: 
  version: 1.4
  last-reviewed: 2026-10-01
  upstream-version: 3.7
  skill-author: K-Dense Inc.
---

# NetworkX

## Overview

NetworkX is a Python package for creating, manipulating, and analyzing complex networks and graphs. Use this skill when working with network or graph data structures, including social networks, biological networks, transportation systems, citation networks, knowledge graphs, or any system involving relationships between entities.

This skill targets tested NetworkX 3.7 (Python >=3.12, excluding 3.14.1). Several pre-3.0 APIs (`nx.info`, `nx.write_gpickle`, `nx.read_shp`) and the 3.4-era `nx.random_tree` no longer exist — current replacements are used throughout this skill.

Review sources, executed examples, optional-dependency limits, and reproducibility notes are in [references/review.md](references/review.md). Reference snippets are API patterns: supply the stated graph type and input files; they are not one sequential script.

## When to Use This Skill

Invoke this skill when tasks involve:

- **Creating graphs**: Building network structures from data, adding nodes and edges with attributes
- **Graph analysis**: Computing centrality measures, finding shortest paths, detecting communities, measuring clustering
- **Graph algorithms**: Running standard algorithms like Dijkstra's, PageRank, minimum spanning trees, maximum flow
- **Network generation**: Creating synthetic networks (random, scale-free, small-world models) for testing or simulation
- **Graph I/O**: Reading from or writing to various formats (edge lists, GraphML, JSON, CSV, adjacency matrices)
- **Visualization**: Drawing and customizing network visualizations with matplotlib or interactive libraries
- **Network comparison**: Checking isomorphism, computing graph metrics, analyzing structural properties

## Core Capabilities

### 1. Graph Creation and Manipulation

NetworkX supports four main graph types:
- **Graph**: Undirected graphs with single edges
- **DiGraph**: Directed graphs with one-way connections
- **MultiGraph**: Undirected graphs allowing multiple edges between nodes
- **MultiDiGraph**: Directed graphs with multiple edges

Create graphs by:
```python
import networkx as nx

# Create empty graph
G = nx.Graph()

# Add nodes (can be any hashable type)
G.add_node(1)
G.add_nodes_from([2, 3, 4])
G.add_node("protein_A", type='enzyme', weight=1.5)

# Add edges
G.add_edge(1, 2)
G.add_edges_from([(1, 3), (2, 4)])
G.add_edge(1, 4, weight=0.8, relation='interacts')
```

**Reference**: See `references/graph-basics.md` for comprehensive guidance on creating, modifying, examining, and managing graph structures, including working with attributes and subgraphs.

### 2. Graph Algorithms

NetworkX provides extensive algorithms for network analysis:

**Shortest Paths**:

For weighted paths and betweenness, weights represent distances/costs: larger values
make a route less favorable. Similarity, correlation, or interaction strength needs an
explicit scientifically justified conversion before use as distance. Validate the chosen
attribute on every edge and use strictly positive distances for weighted betweenness.

```python
# Same distance model for route and length
G = nx.Graph()
G.add_weighted_edges_from([(1, 2, 1), (2, 5, 1), (1, 5, 5)], weight='distance')
path = nx.shortest_path(G, source=1, target=5, weight='distance')
length = nx.shortest_path_length(G, source=1, target=5, weight='distance')
assert path == [1, 2, 5] and length == 2
```

**Centrality Measures**:
```python
# Degree centrality
degree_cent = nx.degree_centrality(G)

# Betweenness centrality
betweenness = nx.betweenness_centrality(G)

# PageRank
pagerank = nx.pagerank(G)
```

**Community Detection**:
```python
from networkx.algorithms import community

# Specify weight=None for unweighted topology
communities = community.greedy_modularity_communities(G, weight=None)
```

**Connectivity**:
```python
# Check connectivity
is_connected = nx.is_connected(G)

# Find connected components
components = list(nx.connected_components(G))
```

**Reference**: See `references/algorithms.md` for worked API patterns for common algorithms including shortest paths, centrality measures, clustering, community detection, flows, matching, tree algorithms, and graph traversal.

### 3. Graph Generators

Create synthetic networks for testing, simulation, or modeling:

**Classic Graphs**:
```python
# Complete graph
G = nx.complete_graph(n=10)

# Cycle graph
G = nx.cycle_graph(n=20)

# Known graphs
G = nx.karate_club_graph()
G = nx.petersen_graph()
```

**Random Networks**:
```python
# Erdős-Rényi random graph
G = nx.erdos_renyi_graph(n=100, p=0.1, seed=42)

# Barabási-Albert scale-free network
G = nx.barabasi_albert_graph(n=100, m=3, seed=42)

# Watts-Strogatz small-world network
G = nx.watts_strogatz_graph(n=100, k=6, p=0.1, seed=42)
```

**Structured Networks**:
```python
# Grid graph
G = nx.grid_2d_graph(m=5, n=7)

# Random tree (random_tree was removed in NetworkX 3.4)
G = nx.random_labeled_tree(100, seed=42)
```

**Reference**: See `references/generators.md` for representative graph generators including classic, random, lattice, bipartite, and specialized network models with detailed parameters and use cases.

### 4. Reading and Writing Graphs

NetworkX supports numerous file formats and data sources:

**File Formats**:
```python
# Edge list
G = nx.read_edgelist('graph.edgelist')
nx.write_edgelist(G, 'graph.edgelist')

# GraphML (supported scalar attributes; node IDs read as strings by default)
G = nx.read_graphml('graph.graphml')
nx.write_graphml(G, 'graph.graphml')

# GM