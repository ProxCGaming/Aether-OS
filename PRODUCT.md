# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop

## Stack
Electron + React + Vanilla CSS

## Users
Developers and power users interacting with a local, memory-augmented AI agent (Aether) to automate tasks, explore knowledge graphs, and manage long-term episodic memory.

## Product Purpose
To provide the desktop interface for the local Aether agent. Success is observable: the user can see what the agent remembers, what it is doing, and what it is about to do — and can stop or redirect each of the three.

## Positioning
Unlike standard chat interfaces that rely solely on LLM context windows, Aether-OS maintains a persistent local knowledge graph (semantic memory) and chronological event logs (episodic memory), enabling the agent to genuinely learn and retain context over time.

## Operating Context
Users will run the application locally on their desktop. It interfaces directly with the local Aether Python engine (aether_engine).

## Capabilities and Constraints
- Must connect to the local Python Aether engine via API or IPC.
- The UI is dark-themed, with translucent layered panels (glassmorphism) over the chat and dashboard surfaces.
- Built as a standalone desktop executable via Electron.

## Brand Commitments
- Name: Aether-OS
- Voice: Capable, intelligent, out-of-the-way.
- Aesthetic: dark mode; dense, information-first surfaces.

## Evidence on Hand
- Backend architecture exists: `aether_engine` running locally.
- Episodic and Semantic memory logic exists.

## Product Principles
1. **Memory is the core:** The UI should make the agent's memory (graphs, logs) visible and tangible, not hidden.
2. **Local first:** Emphasize speed, privacy, and local execution.
3. **Craft shows in behaviour:** The interface earns trust by responding immediately, never losing typed input, and never showing a control that does nothing.
