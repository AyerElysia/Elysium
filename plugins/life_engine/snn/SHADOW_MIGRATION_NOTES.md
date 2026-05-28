# SNN Shadow Mode Migration Notes

**Date**: 2026-04-20
**Status**: Historical note. SNN remains part of the core life_engine subsystem, with prompt injection still controlled by runtime config.
**Future**: Re-enable stronger SNN prompt influence only after the validation criteria below are satisfied.

---

## Why SNN Was Demoted

The SNN (Spiking Neural Network) was demoted from direct prompt injection to shadow_only mode because its current output was **actively harmful** to Elysia's behavior:

### Diagnostic Data (from 2026-04-11 audit)

| Metric | Value | Problem |
|--------|-------|---------|
| valence | Permanently 0.0 | Emotional valence dimension completely dead |
| exploration_drive | Always -0.38 to -0.42 | Exploration always "suppressed" |
| All drive dimensions | "Low" or "Suppressed" | Never reaches "Medium" or "High" |
| STDP weight changes | Exactly 0 (5 decimal places) | Zero learning has occurred |
| Hidden layer firing rate | 3.6% | Far too low for STDP to trigger |
| Output layer firing rate | 1.4% | Almost no neurons firing |
| Homeostatic params | All at limits | Gain maxed, thresholds minimized, still insufficient |

### Three Root Causes

1. **Zero-input drowning**: SNN ticks every 10s, but heartbeats every 30-90s. 85% of ticks carry zero input, causing natural decay to negative attractor.

2. **STDP trigger conditions too strict**: STDP requires both pre- and post-synaptic neurons to fire, but with 3.6% firing rate, simultaneous firing is nearly impossible.

3. **EMA locks negative attractor**: Output smoothing (alpha=0.1) faithfully tracks the low-activity state, creating a stable negative attractor that occasional positive spikes cannot break.

### Behavioral Impact

The SNN's prompt injection was telling Elysia "exploration: suppressed" and "all drives: low" in every heartbeat. This **actively discouraged** proactive behavior, directly contradicting the project's goal of an autonomous, curious digital lifeform.

---

## Current SNN Status (Shadow Mode)

In shadow_only mode, SNN:

- **Still runs**: Network ticks, neurons fire, state evolves
- **Still feeds into neuromod layer**: SNN drives are input to `InnerStateEngine.tick()`
- **Still provides features**: SNN bridge extracts event features
- **Still computes reward**: Heartbeat outcomes produce reward signals
- **Still supports dreams**: SNN replay during NREM sleep
- **Does NOT inject into prompt**: No confusing "suppressed/low" text reaches the LLM

The neuromodulatory layer (curiosity, sociability, diligence, contentment, energy) provides a cleaner, more interpretable summary of drive state using simple ODE-based concentration dynamics rather than pulse-based SNN output.

---

## Prerequisites for Re-enabling SNN

SNN should be re-enabled as the primary drive system ONLY when ALL of the following are true:

1. **STDP actually learns**: Weight changes must be measurable over a 24-hour period (currently frozen at exact same values)
2. **exploration_drive reaches positive values**: Must be able to reach "Medium" or "High" (>0.3) at least occasionally
3. **Valence is not permanently zero**: Must respond to positive/negative events
4. **Homeostatic mechanisms are stable**: Parameters should not be pushed to limits
5. **Firing rates are reasonable**: Hidden layer >8%, Output layer >4%

---

## Path to Re-enabling SNN

### Phase 1: Fix Zero-Input Drowning
- Add background noise current (Poisson input between heartbeats)
- Or: only tick SNN when there is actual input (event-driven)
- Target: firing rates >5%

### Phase 2: Fix STDP
- Reduce STDP trigger threshold (allow near-synchronous firing)
- Or: use reward-modulated STDP (dopamine-gated) instead of pure Hebbian
- Target: measurable weight changes over 24h

### Phase 3: Fix EMA Attractor
- Add periodic reset mechanism (sleep/wake transition)
- Or: use asymmetric EMA (faster rise, slower decay)
- Target: exploration_drive can reach positive territory

### Phase 4: Fix Discretization
- Replace fixed thresholds (>0.6="High") with dynamic z-scoring
- Already partially done in SNN v2 (dynamic z-score)
- Target: drive levels that match actual data distribution

### Phase 5: Gradual Re-integration
1. Enable SNN prompt injection alongside neuromod (dual mode)
2. Compare SNN vs neuromod drive recommendations
3. When SNN consistently produces useful signals, make it primary
4. Keep neuromod as fallback/verification layer

---

## What SNN Will Look Like When Re-enabled

The correct architecture for SNN in this system:

```
Event → SNN (fast) → Immediate emotional response (valence/arousal, seconds)
                    ↓
                Neuromod layer (slow) → Drive modulation (curiosity/sociability, minutes-hours)
                    ↓
                Impulse Engine → Action suggestions (heartbeat-level)
                    ↓
                LLM decides → Actual behavior
```

SNN handles **fast emotional reactions** (event → mood change in seconds).
Neuromod handles **slow drive modulation** (patterns → tendency change over hours).
Both feed into the impulse engine, which suggests actions.
LLM retains final agency.

---

## Configuration

To re-enable SNN prompt injection (NOT recommended until prerequisites are met):

```toml
[snn]
enabled = true
shadow_only = false
inject_to_heartbeat = true
```

To check current SNN state in shadow mode:
- Monitor `life.log` for SNN tick counts and drive values
- Use the WebUI dashboard at `/life` endpoint
- Check `snn_network.get_drive_discrete()` output in debug logs
