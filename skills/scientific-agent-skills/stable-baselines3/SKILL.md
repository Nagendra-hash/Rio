---
name: stable-baselines3
description: Trains and evaluates single-agent reinforcement learning with Stable Baselines3 (PPO, SAC, DQN, TD3, DDPG, A2C), Gymnasium custom environments, vectorized rollouts, callbacks, and checkpoint normalization. Applies to reproducible RL experiments, continuous control, discrete actions, and SB3-Contrib recurrent or masked policies.
license: MIT license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.10+, PyTorch >= 2.8, and stable-baselines3 2.9.0. Gymnasium environments; optional extras for TensorBoard and Atari (ale-py).
metadata: 
  version: 2.0
  last-reviewed: 2026-10-01
  upstream-version: 2.9.0
  skill-author: K-Dense Inc.
---

# Stable Baselines3

## Overview

Stable Baselines3 (SB3) is a PyTorch-based library providing reliable implementations of reinforcement learning algorithms. This skill provides comprehensive guidance for training RL agents, creating custom environments, implementing callbacks, and optimizing training workflows using SB3's unified API.

**Current upstream:** SB3 **2.9.0** (June 15, 2026). Docs: [stable-baselines3.readthedocs.io](https://stable-baselines3.readthedocs.io/en/v2.9.0/).

## Installation

Tested against **stable-baselines3 2.9.0**. Requires **Python 3.10+** (3.9 dropped in 2.8.0) and **PyTorch >= 2.8**.

```bash
# Basic installation
uv pip install "stable-baselines3==2.9.0"

# With extra dependencies (TensorBoard, ale-py for Atari, etc.)
uv pip install "stable-baselines3[extra]==2.9.0"
```

The 2.9.0 release supports Gymnasium >=0.29.1,<2.0; this review exercised
Gymnasium 1.3.0, PyTorch 2.14.1 and Python 3.13 on CPU. pandas/matplotlib
are now optional extras. Installation requires network unless packages are cached;
local RL training needs no credentials or service endpoints.

On zsh, quote brackets: `uv pip install 'stable-baselines3[extra]==2.9.0'`.

For MuJoCo continuous-control benchmarks:

```bash
uv pip install "gymnasium[mujoco]"
```

Check your version:

```python
import stable_baselines3
print(stable_baselines3.__version__)
```

## Related Projects

- **[SB3-Contrib](https://github.com/Stable-Baselines-Team/stable-baselines3-contrib)**: experimental algorithms (MaskablePPO, CrossQ, QR-DQN, RecurrentPPO) — separate `sb3-contrib` package
- **[RL Baselines3 Zoo](https://github.com/DLR-RM/rl-baselines3-zoo)**: pre-trained agents, hyperparameters, training scripts
- **[SBX](https://github.com/araffin/sbx)**: SB3 + JAX implementations for users who prefer JAX over PyTorch

## Core Capabilities

### 1. Training RL Agents

**Basic Training Pattern:**

```python
import gymnasium as gym
from stable_baselines3 import PPO

# Create environment
env = gym.make("CartPole-v1")

# Initialize agent (device="cpu" is often faster for MlpPolicy on small envs)
model = PPO("MlpPolicy", env, verbose=1, device="cpu", seed=0)

# Train the agent
model.learn(total_timesteps=10000)

# Save the model
model.save("ppo_cartpole")

# Load the model (without prior instantiation)
model = PPO.load("ppo_cartpole", env=env, device="cpu")
env.close()
```

**Important Notes:**
- `total_timesteps` is a lower bound; actual training may exceed this due to batch collection
- Call the class method `PPO.load(...)` and keep the returned new model
- The replay buffer is NOT saved with the model to save space

**Algorithm Selection:**
Use `references/algorithms.md` for detailed algorithm characteristics and selection guidance. Quick reference:
- **PPO/A2C**: General-purpose, supports Box, Discrete, flat MultiDiscrete and MultiBinary actions, good for multiprocessing
- **SAC/TD3**: Continuous control, off-policy, sample-efficient
- **DQN**: Discrete actions, off-policy
- **HER**: Replay-buffer strategy for goal-conditioned off-policy tasks

See `scripts/train_rl_agent.py` for a complete training template with best practices.

### 2. Custom Environments

**Requirements:**
Custom environments must inherit from `gymnasium.Env` and implement:
- `__init__()`: Define action_space and observation_space
- `reset(seed, options)`: Return initial observation and info dict
- `step(action)`: Return observation, reward, terminated, truncated, info
- `render()`: Visualization (optional)
- `close()`: Cleanup resources

**Key Constraints:**
- Default CNN image preprocessing expects `np.uint8` in range [0, 255]
- Use channel-first format when possible (channels, height, width)
- SB3 normalizes images automatically by dividing by 255
- For pre-normalized float images, use channel-first layout and `policy_kwargs={"normalize_images": False}`
- SB3 does NOT support `Discrete` or `MultiDiscrete` spaces with `start!=0`

**Validation:**
```python
from stable_baselines3.common.env_checker import check_env

check_env(env, warn=True)
```

See [the template](scripts/custom_env_template.py) and [environment guide](references/custom_environments.md).
The template now observes both agent and random goal coordinates, shape `(4,)`;
old shape `(2,)` checkpoints require retraining. `gym.make("CustomEnv-v0")` adds
the 100-step time limit; direct `CustomEnv()` does not. `check_env` checks API
consistency, not Markov sufficiency, reward correctness or learnability.

### 3. Vectorized Environments

**Purpose:**
Vectorized environments run multiple environment instances in parallel, accelerating training and enabling certain wrappers (frame-stacking, normalization).

**Types:**
- **DummyVecEnv**: Sequential execution on current process (for lightweight environments)
- **SubprocVecEnv**: Parallel execution across processes (for compute-heavy environments)

**Quick Setup:**
```python
from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env

# DummyVecEnv batches 4 lightweight environments sequentially.
env = make_vec_env("CartPole-v1", n_envs=4, seed=0)
try:
    model = PPO("MlpPolicy", env, verbose=1, device="cpu")
    model.learn(total_timesteps=25000)
finally:
    env.close()
```

**Off-Policy Optimization:**
With step-based `train_freq`, `gradient_steps=-1` matches gradient updates to
collected transitions (`train_freq * n_envs`) after warmup. This changes compute
and reuse of data; benchmark it rather than assuming it is always faster.
SubprocVecEnv creation belongs under a main guard in a Python file.

**API Differences:**
- `reset()` returns only observations (info available in `vec_env.reset_infos`)
- `step()` returns 4-