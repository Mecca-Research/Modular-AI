# Vertical-GAA BPU/GPC Proposal

- Source: https://chatgpt.com/c/6a525276-9138-83ea-aa31-2c0136a474e7?src=history_search
- Capture date: 2026-10-12
- Messages captured: 18
- Capture: complete displayed conversation from its beginning through its latest message.
- Formatting: original wording preserved; rendered formatting converted to Markdown.

## Archive notes

- 9 turns captured with contiguous indices 0–8.
- Message IDs retained; original wording preserved from rendered content and identity-validated source copies.

---

## Message 1 — User

<!-- Message ID: f97aec41-880a-4cad-aaaa-440846f83286; Turn index: 0 -->

Cutting out the exotic memristors, we want to build cheap and highly inference capable Binary Processing Unit (BPU) and Graphic Processing Core (GPC) technology. New AI processing chips for the market. Let's research and expand on the following proposal: "The industry creates Horizontal Nanosheets (H-GAA) for 2nm to save horizontal space. But for a Graph Processing Core (GPC) at 28nm, you should build Vertical Nanowire Gate-All-Around (V-GAA) transistors.Here is the blueprint for a Vertical-GAA Binary Processing Unit that can be manufactured on standard ASML DUV machines (cheap) while retaining the electrostatic superpowers of GAA.1. The Core Concept: Vertical Channel, Not Vertical ScalingIn a standard planar or FinFET chip, the "Gate Length" (the performance-defining feature) is drawn by the lithography machine. To get a 5nm gate, you need a $150M EUV machine.In a Vertical GAA, the current flows up a silicon pillar. The "Gate Length" is defined by the thickness of the metal layer deposited on the sidewall, not by the lithography beam width.The Hack: You can use a cheap 1990s-era deposition tool to lay down a precise 10nm layer of metal. Suddenly, you have a transistor with "5nm-like" electrostatic control, but you built it using a cheap 28nm or 45nm lithography mask.The Result: You get the perfect on/off switching and low leakage of modern GAA, but at the price point of a Raspberry Pi chip. The New "Primitive Cell": 3D Logic-in-MemorySince we removed the memristors, we replace them with a Vertical SRAM (V-SRAM) or Gain Cell design that fuses storage and compute.The "Sky-Scraper" Bit Cell: Instead of spreading out 6 transistors flat on the silicon (taking up huge space), you build a single vertical pillar that acts as the switch. You stack the capacitor or storage latch directly on top of the pillar.Binary Processing Capability: Because these vertical pillars have a tiny footprint (4F² vs 150F² for standard SRAM), you can pack millions of simple, 1-bit ALU (Arithmetic Logic Units) directly next to the memory. This is perfect for Binary Neural Networks (BNNs), where weights are just -1 or +1. This approach is vastly easier to manufacture than the hybrid memristor design.No Exotic Materials: No Hafnium Oxide switching layers or weird thermal budgets. It is just Silicon, Silicon Dioxide, and Metal—standard ingredients every fab (TSMC, GlobalFoundries, UMC, SMIC) has used for 30 years.Tooling: You do not need EUV. You can print the pillars using standard ArF Immersion Lithography. The complexity shifts from "printing small lines" to "etching tall holes," which is exactly what the industry mastered for 3D NAND Flash (SSDs). our end product is a Binary Graph Processor specialized for inference, not training.Massive Parallelism: At 28nm with vertical stacking, you can fit thousands of "dumb" cores that only do XOR and POPCOUNT operations (the core math of binary AI).Ultra-Low Power: Because of the GAA structure, you can lower the voltage to near-threshold (0.4V) without the leakage current killing you.The Catch: Vertical transistors are slower than horizontal ones (lower electron mobility). This chip won't run at 4 GHz like an Intel i9. It will run at 500 MHz—but you will have 10,000 of them running in parallel for pennies.

---

## Message 2 — Assistant

<!-- Message ID: 9abb29f2-2d11-4b3d-b699-8c844b99f759; Turn index: 0 -->

# Vertical-GAA BPU/GPC: the architecture survives, but the fabrication story needs sharpening

This is a **stronger commercial direction than the original memristor hybrid**. Removing the resistive devices eliminates analog programming variability, endurance management, ADC-heavy sensing, verify-and-retry loops, and a large portion of the process-integration risk.

The core insight is legitimate:

> Use mature lithography to define the horizontal footprint, then use deposition, epitaxy, spacers, and etching to define smaller critical dimensions vertically.

But this is really **two coupled projects**:

1. A marketable binary graph-inference processor that can be built now in an ordinary 28 nm CMOS process.
2. A custom vertical-GAA transistor and memory process that could later shrink and improve that processor.

Trying to launch both simultaneously would turn a cheap accelerator into a semiconductor moon landing. 🚀

---

## 1. Physics audit of the proposal

| Proposal claim | Assessment | Corrected engineering interpretation |
| --- | --- | --- |
| Vertical GAA decouples gate length from horizontal lithographic footprint | **Correct** | The gate length extends vertically and can be controlled by epitaxial thickness, deposited films, spacers, and etch-back rather than by the printed horizontal gate dimension. |
| A 28 nm DUV process can create a much shorter effective gate | **Partly correct** | DUV can define pillar position and pitch, while sub-lithographic vertical dimensions are created by deposition and etching. But this is a custom process, not an ordinary 28 nm PDK option. |
| A 10 nm metal film creates a 10 nm gate | **Too simplified** | The vertical extent of the gate stack defines $L_G$. The radial gate-metal thickness mainly affects resistance and fill. Gate dielectric thickness, spacer thickness, gate height, channel geometry, and source/drain alignment are separate parameters. |
| It produces “5 nm-like” performance | **Overstated** | It may provide advanced-node-like electrostatic control, but not necessarily advanced-node density, contact resistance, wiring density, capacitance, drive current, yield, or frequency. |
| It needs only silicon, SiO₂, and metal | **Not advisable** | Resistive-switching HfOₓ can be removed, but high-k dielectrics and metal-gate stacks should remain. Mature 28 nm logic already uses high-k metal-gate technology. |
| Vertical SRAM can reach 4F² | **Not credible for a complete SRAM cell** | 4F² is associated with extremely compact DRAM or flash-like cells. A static latch still needs multiple transistors, contacts, rails, and read/write access. |
| Vertical transistors are inherently slower | **Not fundamentally true** | Performance depends on channel orientation, contact resistance, strain, capacitance, nanowire width, and process quality. Vertical devices can outperform lateral alternatives in some regimes. |
| 0.4 V operation follows automatically from GAA | **No** | GAA helps electrostatics, but SRAM stability, threshold variation, sense amplifiers, interconnect delay, and voltage droop determine system $V_{\min}$. |

IBM’s VTFET work demonstrates the real advantage: gate length, spacer dimensions, source/drain contacts, and device width can be optimized more independently than in lateral devices. IBM demonstrated CMOS VTFETs below a 45 nm contacted gate pitch with sub-30 mV DIBL and functional ring oscillators. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fresearch.ibm.com&size=32&drop_404_icon=true)IBM Research+1](https://research.ibm.com/publications/vertical-transport-nanosheet-technology-for-cmos-scaling-beyond-lateral-transport-devices)

Imec similarly describes vertical GAA as a route where gate length is defined vertically and is no longer constrained directly by the planar footprint. However, it explicitly calls for early process-design co-optimization rather than treating the device as a drop-in modification to a standard node. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.imec-int.com&size=32&drop_404_icon=true)imec+1](https://www.imec-int.com/en/imec-magazine/imec-magazine-september-2017/the-vertical-nanowire-fet-enabler-of-highly-dense-srams)

### The crucial geometric correction

For a vertical cylindrical or nanosheet transistor:

$$
P_{XY}=\text{lithographically defined pillar pitch}
$$

$$
L_G=\text{vertical gate extent defined by deposition/epitaxy/etch}
$$

$$
t_{\mathrm{EOT}}=\text{radial electrical thickness of the gate dielectric}
$$

$$
D_{\mathrm{CH}}=\text{pillar or channel diameter defined by patterning and oxidation/etch}
$$

Therefore:

$$
L_G \not\equiv t_{\mathrm{metal}}
$$

The metal thickness, gate length, dielectric thickness, and channel diameter are separate knobs. That distinction prevents the process specification from becoming physically ambiguous.

---

## 2. The 3D-NAND analogy is useful, but only halfway

The proposal is right that vertical logic can reuse expertise in:

- high-aspect-ratio etching,
- conformal dielectric deposition,
- atomic-layer deposition,
- cylindrical structures,
- fill and chemical-mechanical polishing,
- self-aligned spacers.

But 3D NAND tolerates device characteristics that high-performance logic cannot. Logic requires tightly controlled nFET and pFET thresholds, low-resistance source/drain contacts, high-quality crystalline channels, predictable parasitic capacitance, and low cell-to-cell variation.

A recent review of vertical transistor fabrication notes that channel-last, hole-based flows resemble 3D NAND, but forming high-quality single-crystal channels becomes increasingly difficult as aspect ratio rises. Top-down vertical processes are more CMOS-compatible, yet gate-length variation and gate-to-source/drain alignment remain substantial challenges. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Facademic.oup.com&size=32&drop_404_icon=true)OUP Academic](https://academic.oup.com/nsr/article/11/3/nwae008/7512206)

So the correct statement is:

> Vertical GAA can use DUV lithography and memory-style vertical process techniques, but it still requires an advanced custom logic process module.

It removes the need for EUV. It does **not** remove the need for serious process development.

---

# 3. The new architecture: BPGC-28

I would formalize the product as:

## Binary Processing and Graph Core, 28 nm

The architecture has two distinct hardware personalities:

### BPU: Binary Processing Unit

Optimized for:

- binary convolution,
- binary matrix multiplication,
- binary fully connected layers,
- binary attention projections,
- Hamming-distance search,
- similarity matching,
- bitset operations,
- threshold networks.

### GPC: Graph Processing Core

Optimized for:

- sparse edge traversal,
- gather/apply/scatter,
- adjacency-list decoding,
- segment reduction,
- frontier management,
- irregular memory prefetch,
- graph neural-network aggregation,
- routing and dependency execution.

The distinction is essential. **Graph processing is not just XOR and POPCOUNT.** XOR/POPCOUNT accelerates binary feature transformation. Graph execution also needs address generation, sparse memory access, reductions, queues, synchronization, and routing.

Research on GNN accelerators consistently separates memory-intensive edge-centric work from arithmetic-intensive vertex-centric work. GRIP, for example, uses parallel prefetch and reduction engines for edges and a matrix engine with dedicated weight memory for vertex transformations; its evaluated implementation targeted 28 nm inference silicon. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/abs/2007.13828)

---

## 4. The binary datapath

For binary vectors encoded as $0/1$:

$$
m=\operatorname{POPCOUNT}\left(\operatorname{XNOR}(X,W)\right)
$$

For corresponding values in $\{-1,+1\}$:

$$
X\cdot W=2m-N
$$

Equivalently, using mismatch count:

$$
X\cdot W=N-2\operatorname{POPCOUNT}\left(\operatorname{XOR}(X,W)\right)
$$

Batch normalization and sign activation can then be compiled into one threshold:

$$
Y=
\begin{cases}
1,& X\cdot W\geq \theta\\
0,& X\cdot W<\theta
\end{cases}
$$

A practical BPU lane therefore contains:

`256-bit weight read
        │
256-bit activation register
        │
256 parallel XNOR gates
        │
carry-save / compressor popcount tree
        │
9-bit population count
        │
16–24-bit layer accumulator
        │
threshold + scale + clamp
        │
packed binary output`

A previous fully digital XNOR engine demonstrated the architectural value of this approach using SRAM and standard-cell memory. Its 22 nm implementation reported 21.6 fJ per binary operation at 0.4 V, although that result should be treated as a design reference, not as a guaranteed operating point for a new 28 nm chip. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/abs/1807.03010)

---

# 5. Do not place a complete ALU beside every bit

The “one vertical pillar equals one bit plus one ALU” concept is visually elegant, but transistor-expensive.

A full per-bit ALU would consume more area in local logic and wiring than the storage cell itself. The efficient arrangement is:

- one storage cell per bit,
- one small read or compare path per bit,
- shared POPCOUNT and accumulation logic per 128- or 256-bit column group.

That produces **compute density without logic barnacles**.

## Recommended primitive hierarchy

### Level 0: Storage bit

Either:

- 6T vertical SRAM,
- 8T vertical SRAM with isolated compute read port,
- 2T or 4T gain-cell eDRAM.

### Level 1: Binary compare slice

A local XNOR or XOR function placed in the read path.

### Level 2: Vector lane

128 or 256 compare slices feeding one POPCOUNT tree.

### Level 3: BPU tile

Multiple vector lanes, local memory, accumulators, threshold units, DMA, and scheduler.

This is closer to manufacturable SRAM computing-in-memory designs, which share sensing and reduction circuitry across columns rather than replicating a complete arithmetic unit inside every bit cell.

---

# 6. The memory choice

## Option A: 8T SRAM compute cell

**Best first product.**

Structure:

- conventional cross-coupled 6T latch,
- separate 2T read/compute port,
- non-destructive read,
- no refresh,
- high noise margin,
- straightforward parity and ECC.

Advantages:

- reliable,
- easy to model,
- predictable timing,
- suitable for low-voltage experimentation,
- ideal for weight storage.

Disadvantage:

- larger than 6T SRAM or gain-cell memory.

## Option B: 2T or 4T gain-cell eDRAM

**Best density experiment.**

A gain cell stores charge dynamically rather than using a six-transistor latch. Logic-compatible 4T gain-cell arrays have been fabricated in 28 nm processes, including an 800 MHz implementation, and 28 nm FD-SOI gain-cell work has demonstrated millisecond-class retention under selected conditions. The cost is refresh control and significant retention variation across process, voltage, and temperature. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fieeexplore.ieee.org&size=32&drop_404_icon=true)IEEE Xplore+2](https://ieeexplore.ieee.org/document/8094587/?utm_source=chatgpt.com)

Good uses:

- activation buffers,
- intermediate graph features,
- edge metadata,
- temporary frontier storage,
- short-lived inference data.

Less ideal uses:

- always-on model weights,
- safety-critical state,
- long idle retention without refresh.

## Option C: Hybrid SRAM plus gain-cell

This is the strongest long-term arrangement:

- SRAM for control state, thresholds, frequently reused weights, queues, and metadata.
- Gain-cell eDRAM for bulk activations and transient graph buffers.

Simulation work on mixed SRAM/eDRAM systems has reported substantial area and energy reductions compared with all-SRAM designs, although the exact benefit is highly workload- and process-dependent. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/html/2312.03559v1?utm_source=chatgpt.com)

---

## The 4F² correction

At a literal $F=28\text{ nm}$:

$$
4F^2=4(0.028\ \mu m)^2=0.003136\ \mu m^2
$$

TSMC reported a 28 nm high-density 6T SRAM bitcell of approximately:

$$
0.127\ \mu m^2
$$

That is roughly 40 times larger than literal $4F^2$. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tsmc.com&size=32&drop_404_icon=true)TSMC](https://www.tsmc.com/download/ir/annualReports/2008/pdf_e/e_5_2.pdf)

Vertical SRAM research points toward **incremental but meaningful scaling**, not a 40-fold collapse. Imec projected about a 30% cell-area reduction for vertical nanowire 6T SRAM relative to lateral nanowire SRAM, and simulated a further 39% per-bit reduction by stacking two SRAM cells vertically. Another vertical surrounding-gate SRAM study targeted approximately $0.0205\ \mu m^2$ at N5-class dimensions, still using a complete six-transistor cell. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.imec-int.com&size=32&drop_404_icon=true)imec+2](https://www.imec-int.com/en/imec-magazine/imec-magazine-september-2017/the-vertical-nanowire-fet-enabler-of-highly-dense-srams)

A defensible target is therefore:

> 20–40% memory-area improvement from vertical layout and stacking, not a 30–40× improvement from turning SRAM into a 4F² cell.

---

# 7. GPC tile architecture

`┌─────────────────────────────────────────────┐
│                 GPC TILE                    │
├─────────────────────────────────────────────┤
│ Edge DMA / CSR-CSC decoder                  │
│ Degree classifier and hub splitter          │
│ Gather queue + outstanding memory requests  │
├─────────────────────────────────────────────┤
│ Local weight SRAM                           │
│ Activation SRAM / gain-cell bank            │
│ Node and edge metadata bank                 │
├─────────────────────────────────────────────┤
│ 8 × 256-bit BPU lanes                       │
│ XNOR / XOR / AND / OR                       │
│ POPCOUNT / leading-zero / bit-select        │
├─────────────────────────────────────────────┤
│ Segment SUM / MAX / MIN / OR reduction      │
│ Threshold / batchnorm / activation          │
├─────────────────────────────────────────────┤
│ Scatter engine / frontier generator         │
│ Packet router / NoC interface               │
└─────────────────────────────────────────────┘`

## Graph execution pipeline

$$
\text{FETCH}
\rightarrow
\text{GATHER}
\rightarrow
\text{BINARY TRANSFORM}
\rightarrow
\text{REDUCE}
\rightarrow
\text{UPDATE}
\rightarrow
\text{SCATTER}
$$

The edge engine should support:

- CSR and CSC adjacency formats,
- block-sparse adjacency,
- destination-sorted edge lists,
- degree binning,
- hub-node splitting,
- vertex tiling,
- segmented reduction,
- masked execution,
- asynchronous frontier queues.

The BPU handles the vector mathematics. The GPC keeps it fed.

---

# 8. Proposed instruction primitives

A compact instruction set could include:

`BLOAD.W      load packed binary weights
BLOAD.X      load packed activations
BXNOR.PC     XNOR and population count
BXOR.PC      XOR and population count
BAND.PC      AND and population count
BDOT.ACC     binary dot product and accumulate
BTHRESH      compare accumulator against threshold
BPACK        pack result bits
BGATHER      gather node or feature blocks
BSCATTER     scatter results over edges
BSEG.SUM     segmented sum
BSEG.MAX     segmented maximum
BFRONTIER    generate next active-node frontier
BMASK        apply edge, node, or feature mask`

For commercial usefulness, the datapath should be bit-serial rather than permanently one-bit-only:

$$
\text{precision}\in\{1,2,4,8\}\text{ bits}
$$

A 4-bit multiplication can be decomposed into weighted binary planes:

$$
A\cdot W=
\sum_{i=0}^{3}\sum_{j=0}^{3}
2^{i+j}\operatorname{BDOT}(A_i,W_j)
$$

This preserves the exceptional 1-bit path while allowing first layers, final classifiers, graph attention, normalization, and difficult models to use higher precision.

---

# 9. Transformation from GAADMSF to the CMOS BPGC

The original design divides deterministic GAA control from a stateful memristor data plane, with PROGRAM, VERIFY, READY, READ, FLOW, and FAILED states. It also exposes differential currents, analog weights, programming confidence, variance estimates, and drift counters. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf] [GAADMSF Primitive Cell - Hybrid GAA+Memristor Architecture.pdf]

That partitioning concept remains valuable, but the mechanisms change:

| Original GAADMSF | New BPGC |
| --- | --- |
| 2T2R differential memristor | 6T/8T SRAM or 2T/4T gain cell |
| Resistance polarity | Ordinary stored binary bit |
| Analog differential current | Fully digital XNOR/XOR |
| ADC or analog sense path | POPCOUNT compressor tree |
| PROGRAM pulses | Synchronous memory write |
| VERIFY and retry | Parity/ECC check |
| Drift counter | ECC syndrome counter |
| Memristor wear | SRAM fault map or retention-age map |
| Analog weight mode | Bit-serial 1/2/4/8-bit mode |
| T/F routing rails | Packetized valid/ready NoC |
| DMSF_BURST | BPU_BURST |
| HYBRID_STREAM | GRAPH_STREAM |

The old RTL’s mem_program_enable, verify_margin, current_t_ua, current_f_ua, variance_est, and drift_counter are all memristor-specific and should disappear from the first CMOS implementation. The high-level controller, execution modes, safety override, routing interface, and continuous-flow concept can survive. [gaadmsf_verilog.txt]

## Revised tile state machine

`RESET
  ↓
INIT
  ↓
LOAD ───────────────┐
  ↓                 │
READY               │
  ↓                 │
GATHER               │
  ↓                 │
BINARY_EXEC          │
  ↓                 │
REDUCE                │
  ↓                 │
SCATTER               │
  ↓                 │
COMMIT ──────────────┘`

Optional gain-cell branch:

`READY → REFRESH → READY`

Fault branch:

`ECC_FAIL → RETRY / SPARE_ROW / FAULT`

---

# 10. A plausible first-chip configuration

## BPGC-28E edge accelerator

| Component | Proposed configuration |
| --- | --- |
| Process | Standard 28 nm HKMG CMOS |
| Clock | 400–700 MHz target |
| GPC tiles | 32 |
| BPU lanes per tile | 8 |
| Lane width | 256 bits |
| Local memory per tile | 256 KiB |
| Total local memory | 8 MiB |
| Binary comparison capacity | 65,536 bits/cycle |
| Nominal peak at 500 MHz | 32.8 trillion binary element operations/s |
| Precision | 1, 2, 4, and 8-bit bit-serial |
| Host | Small RISC-V or equivalent controller |
| Interconnect | 2D packet NoC |
| External memory | LPDDR or DDR interface |
| Initial power target | Approximately 3–10 W, pending physical design |

The compute figure is:

$$
32\ \text{tiles}
\times 8\ \text{lanes}
\times 256\ \text{bits}
\times 500\text{ MHz}
=
32.768\times10^{12}
$$

This should be marketed as **32.8 Tbop/s of binary comparisons**, not casually conflated with 32.8 FP16 TOPS.

Using TSMC’s historical $0.127\ \mu m^2$ 28 nm SRAM bitcell as a rough anchor, 8 MiB would occupy about $8.5\text{ mm}^2$ of raw bitcell area before decoders, sense amplifiers, redundancy, routing, and power structures. A complete die in the 40–70 mm² region is therefore conceivable, but only synthesis, SRAM compiler data, and place-and-route can establish the real number. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tsmc.com&size=32&drop_404_icon=true)TSMC](https://www.tsmc.com/download/ir/annualReports/2008/pdf_e/e_5_2.pdf)

---

# 11. Voltage and clock strategy

Do not design the entire chip around an assumed universal 0.4 V supply.

Use multiple voltage and frequency islands:

| Domain | Initial target |
| --- | --- |
| BPU XOR/XNOR datapath | 0.45–0.65 V |
| SRAM array and sense amplifiers | 0.65–0.9 V |
| NoC and graph control | 0.6–0.8 V |
| I/O and memory PHY | Process-specific higher voltage |

The BPU is naturally tolerant of modest frequency because throughput comes from width and replication. A 500 MHz target is sensible, but it should be chosen to simplify timing closure and reduce energy, not because vertical transistors are intrinsically capped there.

IBM’s VTFET results actually suggest that vertical transport can reduce effective capacitance and potentially improve performance or energy compared with scaled lateral devices. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fresearch.ibm.com&size=32&drop_404_icon=true)IBM Research+1](https://research.ibm.com/publications/vertical-transport-nanosheet-technology-for-cmos-scaling-beyond-lateral-transport-devices)

---

# 12. The fabrication roadmap that preserves “cheap”

## Phase 1: Architecture silicon

Use an ordinary commercial 28 nm process:

- standard planar HKMG transistors,
- foundry SRAM macros,
- synthesized XNOR/POPCOUNT lanes,
- no custom transistor,
- no analog compute,
- no vertical memory.

This is the only phase that deserves the word **cheap**. TSMC’s 28 nm generation was built with manufacturing-ready immersion lithography and high-k metal-gate options, demonstrating that the baseline node already provides the required mature DUV ecosystem. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tsmc.com&size=32&drop_404_icon=true)TSMC](https://www.tsmc.com/download/ir/annualReports/2008/pdf_e/e_5_2.pdf)

## Phase 2: Vertical transistor test vehicle

Develop a small custom device chip containing:

- nFET and pFET V-GAA devices,
- inverters,
- NAND/NOR gates,
- ring oscillators,
- 6T SRAM arrays,
- 8T compute-read SRAM,
- contact-chain structures,
- process-control monitors.

Measure:

$$
I_{\mathrm{ON}},\ I_{\mathrm{OFF}},\ SS,\ DIBL,\ V_T,\ R_C,\ C_{\mathrm{eff}}
$$

and:

- SRAM read/write margin,
- minimum operating voltage,
- retention,
- pillar variation,
- gate-length variation,
- thermal behavior,
- yield across wafer.

## Phase 3: Hybrid process decision

Adopt vertical GAA only if measured silicon provides meaningful gains such as:

- at least 20–30% SRAM density improvement,
- lower energy at equal timing,
- acceptable pFET/nFET symmetry,
- robust contacts,
- competitive yield,
- no catastrophic process-cost increase.

## Phase 4: Vertical BPGC

Port the proven BPU/GPC RTL and compiler onto the vertical technology.

The architecture must not depend on the vertical transistor succeeding. The vertical transistor should accelerate an already functioning product, not serve as the single bridge holding the entire castle above the ravine.

---

# Final assessment

The proposed system should be reframed as follows:

> **A fully digital, memory-centric Binary Processing Unit combined with a heterogeneous Graph Processing Core, initially manufactured in mature 28 nm CMOS, with vertical-GAA SRAM and logic pursued as a second-generation density and voltage optimization.**

The winning parts are:

- mature DUV lithography,
- fully digital XNOR/POPCOUNT computation,
- massive moderate-frequency parallelism,
- local memory,
- inference-only optimization,
- separate edge and vertex engines,
- no memristor programming or analog ADC path,
- bit-serial support beyond pure 1-bit models.

The parts to discard are:

- 4F² SRAM claims,
- a complete ALU per memory pillar,
- “5 nm performance from a 28 nm mask” language,
- the assumption that any standard foundry can fabricate custom V-GAA,
- the assumption that GAA automatically guarantees 0.4 V operation,
- the idea that vertical devices are necessarily slow.

The old GAADMSF was a hybrid analog-state machine. This new design becomes something cleaner and potentially far more buildable: **a digital graph foundry, pouring rivers of bits through thousands of narrow, synchronized channels.** ⚙️🧠

---

## Message 3 — User

<!-- Message ID: bbb2198d-eaf8-4226-86dc-634ee5a1409a; Turn index: 1 -->

The system is reduced instruction set by default, should we adopt an approach to a more ARM type architecture to gain even more speed and power than what the default architecture allows?

---

## Message 4 — Assistant

<!-- Message ID: 63b6887e-00d3-418b-80c1-89dfd4957a3c; Turn index: 1 -->

## Yes, but adopt **ARM’s architectural discipline**, not a full ARM-shaped processor everywhere

The most effective design is not to replace the reduced BPU/GPC instruction set with ARM. It is to create a **heterogeneous architecture**:

$$
\text{ARM-like control plane}
+
\text{domain-specific BPU/GPC execution plane}
$$

That combination can raise real performance and energy efficiency because the control processor becomes better at scheduling, addressing, branching, data movement, and operating-system work, while the BPU remains ruthlessly specialized for XNOR, POPCOUNT, thresholding, gather, reduce, and scatter.

A full general-purpose ARM core inside every tile would be a silicon weed garden. It would consume area and power without increasing the number of useful binary operations.

---

# 1. ARM is already a RISC architecture

The distinction is not really:

$$
\text{RISC versus ARM}
$$

It is:

$$
\text{minimal accelerator sequencer}
\quad\text{versus}\quad
\text{mature programmable RISC architecture}
$$

Arm defines its architecture as RISC, with a large uniform register file, register-oriented operation, and load/store memory behavior. Modern Arm processors gain much of their performance from extensions, vector execution, predication, caches, branch handling, and microarchitecture, not from abandoning reduced instruction-set principles. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer+2](https://developer.arm.com/documentation/ddi0487/mb/-Part-A-Arm-Architecture-Introduction-and-Overview/-Chapter-A1-Introduction-to-the-Arm-Architecture/-A1-1-About-the-Arm-architecture?utm_source=chatgpt.com)

Our existing design already contains the seed of the correct separation. It assigns deterministic control, instruction decoding, scalar computation, routing configuration, verification, and error handling to the CMOS control plane, while bulk parallel work executes in the fabric. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

Removing the memristors makes that separation even cleaner:

`Programmable scalar control
          │
          ▼
Graph/Binary command processor
          │
          ▼
Thousands of narrow BPU lanes`

---

# 2. Recommended architecture: BPGC-H

I would call the revised design the:

## Binary Processing and Graph Core, Heterogeneous Architecture

`┌─────────────────────────────────────────────────────────┐
│                 SYSTEM CONTROL LAYER                    │
│                                                         │
│  Arm-compatible or RISC-V management processor          │
│  Boot, drivers, security, memory setup, host interface   │
└───────────────────────┬─────────────────────────────────┘
                        │ Command queues
┌───────────────────────▼─────────────────────────────────┐
│               GRAPH CONTROL CLUSTER                     │
│                                                         │
│  2-wide in-order scalar cores                           │
│  Address-generation engines                             │
│  Branch/predicate handling                              │
│  DMA, descriptor walkers, frontier queues               │
│  BPGC macro-instruction decoder                         │
└───────────────────────┬─────────────────────────────────┘
                        │ Tile programs
┌───────────────────────▼─────────────────────────────────┐
│                BPU/GPC TILE ARRAY                       │
│                                                         │
│  XNOR/XOR → POPCOUNT → ACCUMULATE → THRESHOLD           │
│  Gather → Transform → Segment Reduce → Scatter           │
│  Local SRAM / gain-cell memory                          │
│  Packet NoC                                             │
└─────────────────────────────────────────────────────────┘`

This preserves the previous architecture’s three useful modes:

| Previous mode | New digital equivalent |
| --- | --- |
| GAA_FAST | SCALAR_CONTROL |
| DMSF_BURST | BPU_BURST |
| HYBRID_STREAM | GRAPH_STREAM |

The original HYBRID_STREAM mode already envisioned preprocessing, bulk execution, and postprocessing as separate pipelined stages. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

That becomes:

$$
\text{Address generation}
\rightarrow
\text{Binary vector execution}
\rightarrow
\text{Graph reduction}
\rightarrow
\text{Commit}
$$

---

# 3. Do not make the BPU instruction set “larger”

Make it **more semantically dense**.

There is an important difference.

A bloated ISA might require many transistors for instructions rarely used. A semantically dense ISA lets one instruction launch a large, useful operation:

`BGATHER.REDUCE`

could perform:

1. Read an edge-list descriptor.
2. Gather source-node features.
3. Apply a mask.
4. Perform binary transformation.
5. Segment-reduce by destination node.
6. Write the destination accumulator.

That is one visible architectural instruction, but internally it might generate dozens or thousands of micro-operations.

This is the right kind of “complexity”:

$$
\text{Simple hardware primitives}
+
\text{powerful command semantics}
$$

The external ISA becomes rich, while the internal execution fabric stays reduced.

---

# 4. Three instruction layers

## Layer 1: System ISA

An actual Arm or RISC-V control processor handles:

- boot and firmware,
- memory protection,
- interrupts,
- PCIe or host communication,
- device drivers,
- scheduling,
- security,
- telemetry,
- fault recovery.

This processor does not execute the neural network inner loops.

## Layer 2: BPGC command ISA

This is the programmer-visible accelerator language.

Example command families:

`Memory:
  MLOAD       Load tile memory
  MSTORE      Store tile memory
  MPREFETCH   Prefetch graph or weight blocks
  MDMA        Launch descriptor-based transfer

Binary:
  BXNOR.PC    XNOR plus population count
  BXOR.PC     XOR plus population count
  BDOT        Binary dot product
  BTHRESH     Threshold accumulator
  BPACK       Pack output bits

Graph:
  GGATHER     Gather source features
  GREDUCE     Segment reduction
  GSCATTER    Scatter destination results
  GFRONTIER   Create active-node frontier
  GDEGREE     Classify nodes by degree
  GEXPAND     Expand adjacency list

Fused:
  BDOT.THRESH
  GGATHER.BDOT
  GGATHER.REDUCE
  GTRANSFORM.SCATTER`

## Layer 3: Internal tile micro-operations

The macro-instructions decompose into very small operations:

`SRAM_READ
XNOR_256
POPCOUNT_256
ACCUM_ADD
COMPARE
MASK_WRITE
QUEUE_PUSH
ROUTE_SEND`

These micro-operations need not be visible to software. Their encoding can change between chip generations without breaking compiled programs.

That gives us:

$$
\text{stable ISA}
\rightarrow
\text{generation-specific microcode}
\rightarrow
\text{simple execution machinery}
$$

---

# 5. ARM features worth borrowing

## A. A proper register architecture

The current controller is mostly request signals and state transitions. The Verilog exposes discrete operations such as req_program, req_read, and req_flow, followed by a seven-state controller. [gaadmsf_verilog.txt]

Replace this with a small programmable register machine.

A good Graph Control Core could contain:

- 16 or 32 scalar registers,
- 8 address registers,
- 8 predicate registers,
- 8 command descriptor registers,
- 4 loop registers,
- 4 queue pointers,
- status and fault registers.

There is no need to reproduce all 31 AArch64 general-purpose registers, but the Arm model demonstrates the benefit of a uniform register file rather than a jungle of special-purpose signals. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer](https://developer.arm.com/documentation/102374/0103/Registers-in-AArch64---general-purpose-registers?utm_source=chatgpt.com)

---

## B. Predication

Graph workloads are crawling with irregularity:

- nodes of different degree,
- missing neighbors,
- masked edges,
- padded feature vectors,
- sparse active frontiers,
- conditional updates.

Branching separately for each lane is expensive. Predication lets one instruction operate only on selected lanes:

`P0 = EDGE_VALID & NODE_ACTIVE
GGATHER.P P0, V0, [EDGE_PTR]
BXNOR.PC.P P0, A0, V0, W0`

Arm SVE uses dedicated predicate registers and vector-length-independent execution. That is an excellent model for a graph processor because the same compiled operation can run across different physical lane widths. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer+2](https://developer.arm.com/Architectures/Scalable%20Vector%20Extensions?utm_source=chatgpt.com)

For BPGC:

$$
VL \in \{128,256,512,1024\}\text{ bits}
$$

Software asks for “all active lanes,” rather than assuming one fixed hardware vector width.

---

## C. Streaming vector mode

The BPU should have a streaming state resembling the idea behind Arm’s scalable vector and matrix extensions:

`BSTREAM.START
    configuration
    weight base
    activation base
    output base
    vector length
    precision
    threshold table

BSTREAM.RUN
    repeat count

BSTREAM.STOP`

Once streaming begins:

- instruction fetch mostly sleeps,
- weights advance automatically,
- activation addresses advance automatically,
- POPCOUNT results flow into accumulators,
- thresholds apply automatically,
- outputs pack automatically.

Arm’s scalable extensions demonstrate the value of separating vector length from software assumptions, while SME introduces a streaming-oriented execution state for data-intensive work. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer+1](https://developer.arm.com/documentation/102340/0100/Introducing-SVE2?utm_source=chatgpt.com)

Our implementation would be binary-specific and much smaller.

---

## D. Combined address-generation forms

ARM-like addressing can save considerable controller work:

`MLOAD V0, [A0], #32`

means:

1. Load 32 bytes from address A0.
2. Increment A0 by 32.

For graph execution:

`GGATHER V0, [EDGE_PTR++], [FEATURE_BASE]`

could fetch an edge index, calculate the feature address, gather the feature vector, and advance the edge pointer.

Useful addressing modes include:

- base plus immediate,
- base plus scaled index,
- post-increment,
- circular buffer,
- descriptor-relative,
- compressed sparse-row traversal,
- paired loads and stores.

A64 is fundamentally a load/store architecture, but its addressing forms make those loads and stores expressive. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer+1](https://developer.arm.com/documentation/102374/0103/Loads-and-stores?utm_source=chatgpt.com)

---

## E. Zero-overhead loops

A naïve controller repeatedly executes:

`SUB counter
CMP counter, 0
BRANCH not_zero`

For BPU loops, use hardware loop registers:

`LOOP0.SET 1024, kernel_end

kernel_start:
    GGATHER.BDOT ...
    GREDUCE ...
kernel_end:`

The loop controller returns automatically without fetching branch, compare, and decrement instructions each iteration.

This directly saves:

- instruction fetch energy,
- decoder activity,
- branch-predictor activity,
- register-file accesses,
- control bubbles.

For repeated inference kernels, this matters more than decorating the ISA with dozens of scalar arithmetic instructions.

---

## F. Conditional select instead of branches

Use operations such as:

`CSEL    R0, R1, R2, P0
PMERGE  V0, V1, V2, P0`

These choose between two values without redirecting instruction flow.

That is especially useful for:

- activation thresholds,
- optional residual paths,
- node-state updates,
- frontier insertion,
- sparse masks.

---

## G. Saturating and packed arithmetic

Even a binary processor needs supporting integer mathematics:

- 8-bit and 16-bit accumulation,
- threshold values,
- degree normalization,
- quantization,
- activation scaling,
- graph attention metadata.

Include:

`ADD.SAT8
ADD.SAT16
SHIFT.ROUND
MIN
MAX
CLAMP
BITFIELD.EXTRACT
BITFIELD.INSERT
LEADING.ZERO
POPCOUNT`

These are small circuits with outsized utility.

---

# 6. The ideal control microarchitecture

I would not use a large out-of-order CPU for the Graph Control Core.

Use:

## Two-wide in-order execution

Each cycle, it can issue combinations such as:

`Scalar/branch + memory
Scalar/branch + BPU command
Address generation + queue operation`

Suggested pipeline:

`FETCH
  ↓
DECODE / PAIR
  ↓
REGISTER READ
  ↓
EXECUTE / ADDRESS GENERATE
  ↓
MEMORY / COMMAND DISPATCH
  ↓
WRITEBACK`

Add:

- small branch target buffer,
- small instruction cache,
- scratchpad data memory,
- non-blocking loads,
- 8–16 outstanding memory requests,
- decoupled command queue,
- hardware loop buffer,
- descriptor prefetcher.

This is much closer to an efficient Cortex-M or compact Cortex-R-style philosophy than to a phone-class Cortex-A processor.

---

# 7. Where actual ARM IP would help

There are two valid implementation paths.

## Path A: Actual Arm management core

Use a small Arm core for:

- firmware,
- security,
- interrupts,
- driver compatibility,
- device management.

Then connect the BPGC through:

- memory-mapped registers,
- command queues,
- shared coherent or non-coherent memory,
- doorbell interrupts,
- DMA descriptors.

Selected Cortex-M processors support Arm Custom Instructions through the Custom Datapath Extension, allowing application-specific operations to access processor registers directly. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdeveloper.arm.com&size=32&drop_404_icon=true)Arm Developer+2](https://developer.arm.com/Architectures/Arm%20Custom%20Instructions?utm_source=chatgpt.com)

A custom instruction could launch a BPGC operation:

`bpgc_launch(command_descriptor);`

However, the instruction should launch a long-running accelerator command. It should not try to squeeze a 256-bit BPU datapath directly through a small scalar register interface.

## Path B: ARM-inspired proprietary control ISA

Build our own small controller with:

- ARM-like registers,
- load/store behavior,
- predicates,
- hardware loops,
- vector commands,
- interrupts,
- memory protection.

This avoids tying the BPU/GPC architecture itself to one CPU license or ecosystem.

A separate compatibility layer can support:

- Arm host,
- RISC-V host,
- x86 host,
- embedded microcontroller host.

The accelerator command stream remains identical.

---

# 8. What would actually increase performance?

The gain would not come from “more instructions.” It would come from removing control bottlenecks.

The critical utilization equation is:

$$
U_{\mathrm{BPU}}
=
\frac{T_{\mathrm{compute}}}
{T_{\mathrm{compute}}+
T_{\mathrm{memory\ stall}}+
T_{\mathrm{control}}+
T_{\mathrm{synchronization}}}
$$

The architectural objective is not merely to make T_compute faster. The BPU is already extremely wide. The objective is to reduce:

$$
T_{\mathrm{memory\ stall}}
+
T_{\mathrm{control}}
+
T_{\mathrm{synchronization}}
$$

ARM-inspired features help by:

- generating addresses while computation continues,
- prefetching edges and features,
- predicating irregular lanes,
- reducing branch overhead,
- launching entire kernels with one command,
- overlapping gather, compute, reduction, and scatter,
- keeping the BPU fed.

A faster XNOR gate is useless when 70% of the lanes are waiting for graph data.

---

# 9. What would actually reduce power?

Power approximately follows:

$$
P_{\mathrm{dynamic}}
=
\alpha C V^2 f
$$

A richer architecture can either reduce or increase power.

It reduces power when it:

- fetches fewer instructions,
- performs fewer branches,
- moves less data,
- reuses local weights,
- fuses operations,
- sleeps the scalar controller during streaming,
- issues one command for hundreds of tile cycles.

It increases power when it adds:

- large caches,
- wide speculative execution,
- reorder buffers,
- oversized register files,
- complex branch prediction,
- unnecessary coherence.

Therefore the rule should be:

> Add control sophistication only where it raises BPU occupancy or reduces data movement.

No architectural ornaments. Every transistor needs a job description. 🔬

---

# 10. Recommended physical hierarchy

For the proposed 32-tile chip:

`1 system management core
        │
4 graph-control clusters
        │
8 BPU/GPC tiles per cluster
        │
8 × 256-bit lanes per tile`

Each graph-control cluster schedules eight tiles.

This is better than:

- one powerful CPU attempting to micromanage all 32 tiles, or
- one complete CPU duplicated inside every tile.

Each tile still gets a very small local sequencer for:

- command receipt,
- DMA synchronization,
- loop counting,
- fault reporting,
- barrier handling.

The cluster controller handles graph decisions. The tile sequencer handles repetitive execution.

---

# 11. Recommended first-generation ISA shape

## Fixed 32-bit instruction encoding

Use fixed-width instructions for:

- simple decode,
- easy timing closure,
- predictable alignment,
- straightforward compiler generation.

Suggested instruction classes:

| Opcode group | Function |
| --- | --- |
| 0000 | Scalar arithmetic |
| 0001 | Load/store |
| 0010 | Branch/predicate |
| 0011 | Loop and queue control |
| 0100 | DMA and prefetch |
| 0101 | Binary vector |
| 0110 | Graph gather/scatter |
| 0111 | Reduction |
| 1000 | Fused graph-binary |
| 1001 | Synchronization |
| 1010 | Memory management |
| 1011 | Diagnostics and fault handling |
| 1100–1111 | Reserved extensions |

A typical instruction could be:

`31       28 27      24 23      20 19      16 15               0
+----------+----------+----------+----------+------------------+
| class    | opcode   | dst      | src      | immediate/config |
+----------+----------+----------+----------+------------------+`

The command ISA remains reduced in encoding but rich in effect.

---

# 12. The strongest fused instruction

The architectural centerpiece should probably be:

`GGATHER.BDOT.REDUCE`

Conceptually:

$$
Y_v
=
\operatorname{Reduce}_{u\in N(v)}
\left[
N
-
2\operatorname{POPCOUNT}
\left(
X_u\oplus W
\right)
\right]
$$

One command configures:

- adjacency range,
- feature base,
- weight base,
- vector length,
- reduction operation,
- destination accumulator,
- predicate mask,
- threshold rule.

The hardware then streams the neighborhood without returning to the scalar controller after every edge.

That is where the speed and energy advantage becomes architectural rather than merely arithmetic.

---

# Decision

## Adopt an ARM-like control architecture, but keep the BPU/GPC ISA independent

The recommended structure is:

$$
\boxed{
\text{Compact ARM/RISC-style controller}
+
\text{BPGC macro-instruction ISA}
+
\text{reduced internal micro-ops}
+
\text{streaming dataflow engine}
}
$$

Do **not** replace the BPU with a general-purpose ARM processor.

Do **not** put a full CPU in every tile.

Do adopt:

- register-based programming,
- predication,
- scalable vectors,
- zero-overhead loops,
- expressive addressing,
- dual-issue control,
- custom accelerator instructions,
- fused graph commands,
- streaming execution.

The result is not “less RISC.” It is a layered machine where the scalar control plane behaves like a disciplined ARM-class conductor, while the BPU orchestra plays enormous binary chords without waiting for a baton stroke on every bit. 🎼⚙️

---

## Message 5 — User

<!-- Message ID: bbb216f0-8a6f-42f2-b0fb-3243100816a1; Turn index: 2 -->

How can we now add cheap yet efficient matmul and tensor core operations from the BPU and GPC interplay to create cheap yet powerful AI inference compared to the current GPU approach?

---

## Message 6 — Assistant

<!-- Message ID: 04382f61-fedc-4758-882b-501989cb20a5; Turn index: 2 -->

# Add a **Tensor Fusion Matrix Engine**, not a miniature GPU

The cheapest path is to turn the existing BPU lanes into **reconfigurable bit-level tensor machinery**, then let the GPC perform scheduling, gathering, sparsity control, and data movement.

The architecture becomes:

$$
\boxed{
\text{GPC dataflow engine}
+
\text{BPU bit-plane engine}
+
\text{Tensor accumulator fabric}
+
\text{small INT8 fallback array}
}
$$

This avoids duplicating the expensive machinery found around GPU Tensor Cores:

- warp schedulers,
- large vector register files,
- speculative caches,
- thousands of thread contexts,
- broad floating-point support,
- complex coherence,
- large instruction-dispatch structures.

The original GAADMSF already separated deterministic control and scalar work from massively parallel MAC and routing work. We preserve that division, but replace analog memristor MACs with deterministic digital bit-plane arithmetic. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

---

# 1. The new component: Tensor Fusion Matrix Engine

I would call the block the:

## TFME: Tensor Fusion Matrix Engine

`┌──────────────────────────────────────────────────────────┐
│                    GPC CONTROL PLANE                     │
│ Graph scheduler, DMA, gather/scatter, sparse decoding,   │
│ tensor descriptors, tiling, prefetch, synchronization    │
└──────────────────────────┬───────────────────────────────┘
                           │
                 Tensor command stream
                           │
┌──────────────────────────▼───────────────────────────────┐
│                 TENSOR FUSION MATRIX ENGINE              │
│                                                          │
│  BPU Bit Slices                                          │
│  XNOR / AND / XOR / zero masks / POPCOUNT                │
│            │                                             │
│            ▼                                             │
│  Bit-Plane Fusion Network                                │
│  1-bit, 2-bit, 4-bit, ternary, mixed precision           │
│            │                                             │
│            ▼                                             │
│  Carry-Save Accumulator Array                            │
│  INT16 / INT24 / INT32 partial sums                      │
│            │                                             │
│            ▼                                             │
│  Epilogue Engine                                         │
│  scale, bias, clamp, residual, threshold, GELU/SiLU      │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│ GPC REDUCE / SCATTER / ROUTE / NEXT-LAYER PIPELINE       │
└──────────────────────────────────────────────────────────┘`

The TFME is not a separate general-purpose core. It is a **matrix interpretation of the BPU’s existing binary machinery**.

---

# 2. Matmul is already hiding inside the BPU

For unsigned $p$-bit and $q$-bit numbers:

$$
a=\sum_{i=0}^{p-1}2^i a_i
$$

$$
b=\sum_{j=0}^{q-1}2^j b_j
$$

Their dot product can be decomposed into binary dot products:

$$
\mathbf a\cdot\mathbf b
=
\sum_{i=0}^{p-1}
\sum_{j=0}^{q-1}
2^{i+j}
\operatorname{POPCOUNT}
\left(
\mathbf a_i\land\mathbf b_j
\right)
$$

For signed two’s-complement values, define the most significant plane with negative weight:

$$
s_i=
\begin{cases}
2^i,&i<p-1\\
-2^{p-1},&i=p-1
\end{cases}
$$

Then:

$$
\mathbf a\cdot\mathbf b
=
\sum_{i,j}
s_i t_j
\operatorname{POPCOUNT}
\left(
\mathbf a_i\land\mathbf b_j
\right)
$$

The BPU therefore already contains most of a low-precision tensor core:

- binary multiplication through AND or XNOR,
- wide reduction through POPCOUNT,
- sign handling,
- threshold comparison,
- packed storage.

What it lacks is:

1. Matrix tiling.
2. Persistent partial-sum accumulators.
3. Bit-plane scheduling.
4. Weight and activation reuse.
5. Tensor-scale instructions.
6. A fused output stage.

Bit-serial matrix multiplication is a well-established route to variable-precision hardware. More recent work has combined bit-serial MACs with systolic dataflow and runtime precision control from 1 to 16 bits. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/pdf/2603.14988)

---

# 3. Use spatial fusion, not only bit-serial execution

A naïve BPU would execute every bit-plane combination sequentially.

| Operation | Binary plane products |
| --- | --- |
| 1-bit × 1-bit | 1 |
| 2-bit × 2-bit | 4 |
| 4-bit × 4-bit | 16 |
| 4-bit × 8-bit | 32 |
| 8-bit × 8-bit | 64 |

Pure bit-serial processing is excellent at one or two bits but begins to limp at eight bits.

The solution is **bit fusion**.

Individual 1-bit BPU slices dynamically combine to form wider arithmetic units:

`16 independent slices
        │
        ├── 16 × 1-bit operations
        ├──  8 × 2-bit operations
        ├──  4 × 4-bit operations
        └──  1 × 8-bit operation plus support logic`

Bit Fusion demonstrated this general principle with small composable arithmetic elements that spatially combine according to each layer’s precision. The design used a block-oriented ISA to amortize control overhead rather than issuing an instruction for every tiny operation. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/pdf/1712.01507)

For the BPGC, each group of BPU lanes becomes a **Fusion Group**:

`Fusion Group
├── 32 or 64 binary product slices
├── local popcount/compressor tree
├── shift-weight selector
├── signed correction logic
├── carry-save partial-sum register
└── fusion crossbar`

At runtime:

`Precision = 1 bit  → maximum lane parallelism
Precision = 2 bits → four plane combinations
Precision = 4 bits → spatial and temporal fusion
Precision = 8 bits → INT8 anchor array or fused slow path`

---

# 4. Use four tensor execution modes

## Mode 1: BMMA, binary matrix multiply-accumulate

For $\{-1,+1\}$ values:

$$
\mathbf x\cdot\mathbf w
=
2\operatorname{POPCOUNT}
\left(
\operatorname{XNOR}(\mathbf x,\mathbf w)
\right)-K
$$

Instruction:

`BMMA.B1  ACC_TILE, ACT_TILE, WEIGHT_TILE`

This should be the fastest mode.

Uses:

- binary neural networks,
- binary attention projections,
- binary classifiers,
- Hamming search,
- sign-based feature matching,
- binary graph convolution.

No conventional multipliers are required.

---

## Mode 2: TMMA, ternary matrix multiplication

For weights in:

$$
w\in\{-1,0,+1\}
$$

encode each weight using:

- one sign bit,
- one nonzero bit.

The dot product becomes:

$$
\mathbf x\cdot\mathbf w
=
\operatorname{POPCOUNT}
(M\land S_{\mathrm{equal}})
-
\operatorname{POPCOUNT}
(M\land S_{\mathrm{different}})
$$

where $M$ is the nonzero mask.

Equivalent form:

$$
2N_{\mathrm{match}}-N_{\mathrm{nonzero}}
$$

The zero mask prevents unnecessary switching entirely.

`TMMA.T1  ACC_TILE, ACT_TILE, SIGN_TILE, ZERO_TILE`

This is extraordinarily cheap because multiplication becomes:

- mask,
- sign comparison,
- two population counts,
- subtraction.

It also gives the GPC explicit knowledge of zero locations for routing and memory compression.

---

## Mode 3: QMMA, variable-precision integer matmul

This covers:

- INT2 × INT2,
- INT4 × INT4,
- INT4 weights × INT8 activations,
- INT2 weights × INT8 activations.

`QMMA.I4I4
QMMA.I4I8
QMMA.I2I8`

The compiler specifies:

`weight_precision
activation_precision
accumulator_precision
signedness
scale_mode
zero_point_mode`

The TFME then chooses spatial and temporal fusion automatically.

A 2025 accelerator study using bit-serial systolic execution, weight preloading, spatial partial-sum combination, and carry-save reduction reported a synthesized 28 nm peak of 4.09 TOPS and 68.94 TOPS/W for 2-bit operands. Those numbers are design-study results rather than guarantees for our chip, but they demonstrate that this general path is credible on the exact class of process we are considering. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2502.00687)

---

## Mode 4: IMMA, conventional INT8 fallback

Do not force the BPU to spend 64 binary passes on every INT8 multiply.

Add a **small conventional INT8 systolic array** shared by several tiles:

`One 16 × 16 INT8 MAC array
per four BPU/GPC tiles`

Each array contains:

$$
16\times16=256\text{ MAC units}
$$

At 500 MHz:

$$
256\times500\text{ MHz}
=
128\text{ GMAC/s}
$$

Eight such arrays would provide:

$$
8\times128=1.024\text{ TMAC/s}
$$

or approximately:

$$
2.048\text{ TOPS}
$$

when multiplication and addition are counted separately.

This is not meant to compete with a large GPU in raw INT8 throughput. It prevents the architecture from collapsing when a model contains:

- an INT8 first layer,
- an INT8 output layer,
- accuracy-sensitive normalization,
- an unsupported operator,
- a layer that does not quantize well below eight bits.

The silicon remains dominated by cheap low-bit machinery.

---

# 5. Add a LUT mode for low-bit LLM inference

Modern low-bit models frequently combine:

- 2-bit or 4-bit weights,
- 8-bit or 16-bit activations,
- higher-precision accumulators.

Dequantizing every weight into INT8 or FP16 before multiplication wastes the compression advantage.

Instead, add:

## LMMA: lookup-table matrix multiply

For a small activation group:

`a0, a1, a2, a3`

precompute combinations such as:

` 0
+a0
-a0
+a1
-a1
+a0+a1
+a0-a1
...`

A low-bit weight pattern selects the appropriate partial sum from a tiny LUT. The hardware then accumulates that value rather than performing a full multiply.

`LMMA.W2A8
LMMA.W4A8
LMMA.W4A16`

The LUTs can be:

- 16-entry or 32-entry,
- locally generated from activation groups,
- reused across many weights,
- implemented using ordinary SRAM or multiplexers.

LUT Tensor Core research specifically targets mixed-precision GEMM where low-bit weights are multiplied by wider activations. It combines LUT computation, elongated tiles for table reuse, bit-serial support, dedicated instructions, and compiler transformations. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/abs/2408.06003)

### Recommended crossover

| Operand format | Preferred hardware |
| --- | --- |
| B1 × B1 | BPU XNOR/POPCOUNT |
| Ternary × B1/INT8 | BPU sign and zero-mask path |
| INT2 × INT2 | Bit-fused BPU |
| INT4 × INT4 | Bit-fused BPU |
| W2 × A8/A16 | LUT tensor mode |
| W4 × A8/A16 | LUT tensor mode |
| INT8 × INT8 | Small systolic MAC array |
| BF16/FP16 | Narrow compatibility unit or host fallback |

That crossover prevents one arithmetic technique from becoming a universal hammer.

---

# 6. The GPC is what turns arithmetic into a useful tensor processor

The BPU performs cheap arithmetic.

The GPC ensures it does not starve.

For a dense tensor layer, the GPC:

1. Divides matrices into tiles.
2. Selects weight-stationary or output-stationary execution.
3. Prefetches the next activation and weight blocks.
4. Converts values into packed bit planes.
5. Issues tensor commands.
6. Tracks partial sums.
7. Fuses the epilogue.
8. Streams results directly into the next operation.

For a graph layer, it additionally:

1. Reads compressed adjacency structures.
2. Groups edges by destination.
3. Separates low-degree and high-degree nodes.
4. Gathers source-node features.
5. Converts irregular feature accesses into tensor-sized blocks.
6. Dispatches dense blocks to the TFME.
7. Performs segmented reductions.
8. Scatters the results.

This split has strong precedent. GRIP separated memory-intensive edge work from arithmetic-intensive vertex work, using prefetch/reduction engines for edges and a matrix engine with dedicated weight memory for vertex transformations. Its 28 nm place-and-route study reported a 5 W design and substantial latency improvements over its evaluated CPU and GPU baselines. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/abs/2007.13828)

The BPGC extends that concept by making the vertex matrix engine bit-flexible.

---

# 7. Introduce a tensor accumulator file

The BPU’s current POPCOUNT result is too transient. Matmul needs persistent partial sums.

Each tile should contain an output-stationary accumulator file:

`16 × 16 output tile
256 accumulators
24-bit or 32-bit each`

Storage requirement at 32 bits:

$$
256\times32=8192\text{ bits}=1\text{ KiB}
$$

That is tiny compared with the tile SRAM.

Each accumulator receives:

`POPCOUNT result
    ↓
signed correction
    ↓
bit-position shift
    ↓
carry-save accumulation
    ↓
final carry-propagate addition`

Use carry-save representation during the inner loop:

$$
P=P_{\mathrm{sum}}+P_{\mathrm{carry}}
$$

Only perform a full carry-propagate addition when the tile completes. This prevents a long adder chain from becoming the critical path on every bit-plane operation.

---

# 8. Use output-stationary and weight-stationary dataflows

A tensor core is only useful when operands are reused.

## Weight-stationary mode

Best for:

- inference with fixed model weights,
- batch processing,
- convolution,
- repeated graph transformations.

Weights remain in local SRAM or tensor registers while activations stream through.

## Output-stationary mode

Best for:

- long dot products,
- attention products,
- graph neighborhood aggregation.

Partial sums remain in the accumulator file while input and weight chunks stream through.

## Activation-stationary mode

Best for:

- one activation vector reused across many output channels,
- autoregressive token generation,
- matrix-vector multiplication.

The GPC should select the dataflow per operator.

Google’s original inference TPU paired a large matrix multiply unit with a substantial software-managed on-chip memory, demonstrating how domain-specific control and explicit data reuse can remove much of the general-purpose processor machinery. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/1704.04760)

---

# 9. Local memory matters more than another thousand ALUs

A proposed tensor tile could contain:

| Memory region | Example capacity |
| --- | --- |
| Weight scratchpad | 128 KiB |
| Activation scratchpad | 64 KiB |
| Output/partial-sum scratchpad | 32 KiB |
| Graph metadata and descriptors | 32 KiB |
| Total | 256 KiB |

Use at least:

- two weight banks,
- two activation banks,
- two output banks,
- double buffering,
- independent DMA and compute access.

Suggested internal width:

`512-bit weight read
512-bit activation read
256-bit or 512-bit result write`

At 500 MHz, one 512-bit transfer per cycle represents:

$$
64\text{ bytes}\times500\text{ MHz}
=
32\text{ GB/s}
$$

That bandwidth exists locally inside the tile, not across the package.

With 32 tiles, the aggregate internal SRAM bandwidth can be enormous without requiring an equally enormous external memory bus because the same weights and activations are reused locally.

Bit Fusion’s central efficiency argument was not merely cheaper multiplication. It reduced both computation and communication at the precision actually required by each layer. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/pdf/1712.01507)

---

# 10. Fuse the tensor epilogue

A GPU GEMM often produces matrix results and then applies:

- bias,
- scale,
- zero-point correction,
- clamp,
- activation,
- residual addition,
- quantization.

Moving the output to memory between each operation is an energy leak.

Add a small epilogue pipeline:

`ACCUMULATOR
    ↓
per-channel scale
    ↓
bias
    ↓
residual add
    ↓
activation
    ↓
clamp
    ↓
quantize and pack
    ↓
SRAM or next engine`

Supported operations:

`BIAS
SCALE
REQUANTIZE
THRESHOLD
RELU
LEAKY_RELU
CLAMP
RESIDUAL_ADD
GELU_APPROX
SILU_APPROX`

NVIDIA’s own GEMM framework treats the epilogue as a separate, important stage where scaling, clamping, and other elementwise operations can be applied before efficiently writing results. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fdocs.nvidia.com&size=32&drop_404_icon=true)NVIDIA Docs](https://docs.nvidia.com/cutlass/latest/media/docs/cpp/efficient_gemm.html)

For our architecture, the epilogue should be even more aggressively fused because inference operators are known in advance.

---

# 11. Add a cheap nonlinear and normalization engine

Matmul alone cannot execute a modern model.

Add a narrow 16-lane or 32-lane vector assist unit for:

- softmax,
- reciprocal,
- reciprocal square root,
- layer normalization,
- RMS normalization,
- GELU,
- SiLU,
- sigmoid,
- exponent approximation.

Keep it inexpensive through:

- fixed-point arithmetic,
- piecewise-linear approximation,
- small coefficient LUTs,
- Newton-Raphson refinement only when needed,
- shared use across four tiles.

This prevents the host CPU from becoming the bottleneck after every matrix operation.

For transformer inference:

`QKV projection       → TFME
Q × Kᵀ               → TFME
attention mask       → BPU
softmax              → vector assist
attention × V        → TFME
output projection    → TFME
RMSNorm / LayerNorm  → vector assist
MLP                   → TFME`

---

# 12. Add structured and unstructured sparsity support

Current GPU tensor hardware already supports low precision and structured sparsity, so simply supporting INT4 is not enough. NVIDIA’s current inference stack supports formats including FP8, FP4, INT8, and INT4, while its sparse matrix path supports 50% structured sparsity. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.nvidia.com&size=32&drop_404_icon=true)NVIDIA+2](https://www.nvidia.com/en-us/data-center/tensor-cores/)

The BPGC should go further in the workloads it targets.

## Structured sparsity

Support:

- 2:4,
- 1:4,
- block 4×4,
- block 8×8,
- block 16×16.

The BPU stores compact masks and the GPC skips absent blocks.

## Unstructured sparsity

The GPC handles:

- compressed indices,
- zero suppression,
- edge lists,
- variable-length rows,
- queue-based scheduling.

The TFME should not receive individual random nonzeros. The GPC first condenses them into useful blocks.

`Sparse graph/tensor
        ↓
GPC inspect and pack
        ↓
dense or structured-sparse microtile
        ↓
TFME`

This lets the tensor engine remain regular while the GPC absorbs irregularity.

---

# 13. Tensor-level instructions

The ISA should expose whole tiled operations, not individual multiply instructions.

## Configuration

`TCONFIG
    M=16
    N=16
    K=256
    W_FMT=INT4
    A_FMT=INT8
    ACC_FMT=INT32
    DATAFLOW=WEIGHT_STATIONARY
    EPILOGUE=BIAS|SILU|REQUANT`

## Memory operations

`TLOAD.W
TLOAD.A
TSTORE.C
TPREFETCH`

## Matrix operations

`BMMA.B1
TMMA.TERNARY
QMMA.I2I2
QMMA.I4I4
LMMA.W4A8
IMMA.I8I8`

## Fused graph tensor operations

`GATHER.MMA
GATHER.MMA.REDUCE
MMA.SEGMENT_SUM
MMA.SCATTER
GATHER.MMA.REDUCE.SCATTER`

The centerpiece could be:

`GATHER_MMA_REDUCE`

Conceptually:

$$
Y_v=
\operatorname{Reduce}_{u\in N(v)}
\left[
Q(X_u)Q(W)
\right]
$$

One instruction supplies:

- adjacency descriptor,
- source-feature base,
- weight base,
- destination range,
- precision,
- reduction type,
- output scale,
- activation rule.

The GPC executes the graph traversal while the TFME executes matrix arithmetic.

---

# 14. Integrate it into BCIR

This architecture maps naturally onto your Binary Correspondence Intermediate Representation.

Suggested BCIR operators:

`bcir.tensor.config
bcir.tensor.load
bcir.tensor.bmma
bcir.tensor.qmma
bcir.tensor.lmma
bcir.tensor.imma
bcir.tensor.epilogue

bcir.graph.gather
bcir.graph.segment_reduce
bcir.graph.scatter
bcir.graph.gather_mma_reduce

bcir.quant.pack
bcir.quant.unpack
bcir.quant.scale
bcir.quant.requantize

bcir.sparse.mask
bcir.sparse.block_pack
bcir.sparse.zero_skip`

Compiler lowering:

`PyTorch / ONNX / MLIR
        ↓
Tensor and graph analysis
        ↓
Precision assignment
        ↓
Sparsity and dataflow planning
        ↓
BCIR tensor/graph operators
        ↓
BPGC command blocks
        ↓
Tile micro-operations`

The compiler should decide per layer:

---

## Message 7 — User

<!-- Message ID: bbb2153a-423b-4103-8533-7f4e5f907dfd; Turn index: 3 -->

Lets now create the full VHDL implementation for the entire architecture and all the units

---

## Message 8 — Assistant

<!-- Message ID: 26c7ee8e-ebe7-49de-9c6e-5948507141c1; Turn index: 3 -->

# BPGC-T VHDL reference architecture completed ⚙️🧠

I created a complete **VHDL-2008 architectural reference implementation** for the CMOS BPU/GPC tensor-inference system. It preserves the original separation between deterministic control and massively parallel compute, while replacing memristor programming, analog sensing, and resistance-state verification with digital SRAM-oriented execution, BPU arithmetic, tensor accumulation, graph traversal, and packetized command control. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

The modular tile and controller organization also carries forward the useful structural approach from the earlier DMSF VHDL work, rather than attempting to retrofit its memristor-specific signals. [dmsf_cell_vhdl V.2.txt] [dmsf_cell_vhdl V.2.txt]

## Implemented units

The package contains **28 RTL modules**, five testbenches, architecture documentation, instruction-set specifications, build tooling, and combined distribution files.

### BPU and tensor execution

- 256-bit configurable BPU lane
- XNOR plus POPCOUNT binary dot product
- XOR plus POPCOUNT Hamming distance
- AND plus POPCOUNT signed bit-plane arithmetic
- Multi-lane BPU array
- Tensor Fusion Matrix Engine with persistent accumulators
- Ternary $\{-1,0,+1\}$ dot-product engine
- W2A8 low-bit lookup-style MAC engine
- Conventional INT8 MAC fallback array
- Tensor accumulator file
- Bias, scale, residual, activation, clamp, and requantization epilogue
- Fixed-point vector-assist unit

### GPC execution

- CSR edge walker
- Feature gather unit
- Segmented reduction
- Scatter/writeback unit
- Frontier queue
- DMA read engine
- Binary graph pipeline
- Fused gather-compute-reduce-scatter engine

### Control hierarchy

- 128-bit accelerator command format
- Tile command processor
- Cluster command router
- Programmable 32-bit GPC microsequencer
- BPGC tile
- Multi-tile cluster
- Global BPGC top level
- SoC integration shell

The earlier GAA_FAST, DMSF_BURST, and HYBRID_STREAM concepts are translated into scalar control, BPU burst, and graph-stream execution without retaining the obsolete analog write-and-verify path. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

## Accelerator command opcodes

| Opcode | Operation |
| --- | --- |
| 0x10 | Binary matrix multiply, BMMA |
| 0x11 | Quantized bit-plane matrix multiply, QMMA |
| 0x12 | XOR distance |
| 0x13 | INT8 matrix multiply |
| 0x14 | Ternary matrix multiply |
| 0x15 | W2A8 low-bit matrix multiply |
| 0xFF | Fence/synchronization |

The microsequencer supports register operations, branches, counted loops, command launch, completion waiting, immediate construction, and halt. Registers R3:R0 assemble one 128-bit accelerator command.

## Validation completed

- Structural dependency and port-map lint: **passed**
- VHDL syntax parsing for all 28 RTL modules and five testbenches: **passed**
- Arithmetic golden-model smoke tests: **passed**
- ZIP integrity test: **passed**
- Archive SHA-256:

`d0c170599ef770cea9b5c84464fabae7e25c8e93d27ae73e9bfe3624b2dfeff0`

A GHDL compiler was not available in the execution environment, so full compiler elaboration and waveform simulation have **not** yet been executed. The project should therefore be treated as a complete architectural reference RTL package, not tapeout-qualified or foundry-signoff RTL. SRAM macros, clock-domain crossings, physical NoC wiring, DFT, ECC policy, timing constraints, and vendor-specific memory wrappers remain implementation-stage work.

## Files

Download the complete BPGC-T VHDL project

Open the combined synthesizable RTL

Open the combined testbenches

---

## Message 9 — User

<!-- Message ID: bbb21481-346d-46c2-9b26-8712063b323c; Turn index: 4 -->

How do we start integrating high bandwidth memory and process in-memory (PIM) modules to create a unified compute and memory systems on a chip for AI inference, training, and optimized tokenization?

---

## Message 10 — Assistant

<!-- Message ID: b5c1a6a8-4ee6-4fd2-8add-4c4b3fa8fed7; Turn index: 4 -->

# BPGC-UCM: a unified compute-memory architecture

Yes. The next evolution should stop treating memory as a passive warehouse and instead build a **hierarchy of increasingly capable memory tiers**.

One physical clarification matters:

> Once HBM stacks, compute chiplets, and memory-logic dies are combined, the product is more accurately a **unified system-in-package**, not one monolithic system-on-chip.

The architectural illusion presented to software can still be one machine, one address space, and one command model.

Your original design already contained the right genetic material: deterministic control handles scheduling, decoding, safety, and error correction, while massively parallel structures execute dataflow, routing, and MAC operations. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

The new system becomes:

$$
\boxed{
\text{BPGC-UCM}
=
\text{GPC}
+
\text{BPU/TFME}
+
\text{SRAM-PIM}
+
\text{HBM-PIM}
+
\text{Token-PIM}
+
\text{Unified Memory Fabric}
}
$$

---

## 1. Physical architecture

`┌──────────────────────────────────────────────────────────────┐
│                    HOST / SYSTEM CONTROL                     │
│  ARM-like management core, RISC controller, security, OS     │
└────────────────────────────┬─────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────┐
│                     BPGC COMPUTE CHIPLET                     │
│                                                              │
│  GPC clusters                                                │
│  BPU binary lanes                                            │
│  Tensor Fusion Matrix Engines                                │
│  INT8/BF16 compatibility arrays                              │
│  Vector normalization and nonlinear units                    │
│  Tile SRAM and SRAM-PIM                                      │
└────────────────────────────┬─────────────────────────────────┘
                             │ UCIe-compatible die link
┌────────────────────────────▼─────────────────────────────────┐
│                   MEMORY AND PIM CHIPLET                     │
│                                                              │
│  Unified Memory Manager                                      │
│  HBM channel controllers                                     │
│  Address translation and ownership control                   │
│  HBM-PIM command processors                                  │
│  Channel-local vector and binary engines                     │
│  Reduction network                                           │
│  Tokenization and embedding engine                           │
└───────────────┬───────────────────────┬──────────────────────┘
                │                       │
        ┌───────▼───────┐       ┌──────▼────────┐
        │  HBM Stack 0  │       │  HBM Stack 1 │
        │ weights       │       │ KV/optimizer │
        │ embeddings    │       │ activations  │
        └───────────────┘       └───────────────┘
                │
        Optional CXL memory tier
        datasets, cold weights,
        checkpoints, optimizer states`

The first commercial generation should use **ordinary HBM stacks** with PIM implemented in the adjacent memory-controller chiplet. Later generations can move those PIM engines into a customized HBM logic base die or into individual memory-bank structures.

That distinction keeps the first chip buildable.

Samsung’s HBM-PIM work demonstrated the basic model of placing a DRAM-optimized compute engine inside each memory bank to reduce data movement. Samsung reported more than twice the system performance and over 70% lower energy on its evaluated workloads, although those are vendor results and not universal guarantees. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fnews.samsung.com&size=32&drop_404_icon=true)Samsung Global Newsroom](https://news.samsung.com/global/samsung-develops-industrys-first-high-bandwidth-memory-with-ai-processing-power)

---

# 2. Build a PIM ladder

Do not place a complete tensor core inside every DRAM bank. That would create area, power, heat, and synchronization problems.

Instead, create three levels of memory-side execution.

## Level A: SRAM-PIM

Located inside each BPGC tile.

Best operations:

- attention score processing,
- masks,
- thresholds,
- local reductions,
- small matrix-vector operations,
- activation transforms,
- KV-cache tiles,
- frontier and queue operations,
- hot tokenizer tables.

This tier has the lowest latency and greatest flexibility, but the least capacity.

## Level B: HBM bank-side PIM

Located beside HBM banks or pseudo-channels.

Use intentionally small units:

- AND, OR, XOR, XNOR,
- POPCOUNT,
- compare and select,
- INT4 and INT8 dot products,
- additions and subtractions,
- min/max,
- gather and scatter,
- quantization and decompression,
- partial reductions.

The PIM engine reads through the DRAM’s internal bank bandwidth and returns a reduced result rather than entire weight blocks.

## Level C: HBM base-die tensor PIM

Located on the HBM logic base die or memory-controller chiplet.

It can support richer operations:

- GEMV,
- small tiled GEMM,
- BF16 or FP16 vector arithmetic,
- INT32 accumulation,
- normalization statistics,
- optimizer updates,
- embedding reductions,
- KV-cache processing,
- tokenizer vocabulary lookup.

Recent research supports this heterogeneous split. HPIM maps latency-sensitive attention work to SRAM-PIM while assigning large, weight-intensive GEMV operations to HBM-PIM. Its reported speedups are simulation results, but the workload partitioning is highly relevant to BPGC-UCM. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2509.12993?utm_source=chatgpt.com)

---

# 3. Keep the compute die and PIM engines asymmetric

The two sides should not duplicate one another.

| Operation | Preferred execution location |
| --- | --- |
| Large dense GEMM | TFME compute die |
| Binary/ternary matrix operation | BPU or SRAM-PIM |
| Batch-1 weight GEMV | HBM-PIM |
| KV-cache scan | HBM-PIM |
| Attention softmax | SRAM-PIM or vector unit |
| Graph traversal | GPC |
| Embedding lookup | HBM-PIM |
| Optimizer update | HBM-PIM base die |
| Gradient GEMM | TFME |
| Tokenization | Token-PIM |
| Layer normalization | SRAM-PIM/vector unit |
| Host control and branches | Control processor |

The governing rule is:

$$
\text{Send compute to memory}
\quad\text{when}\quad
T_{\text{movement}}>T_{\text{remote execution}}
$$

The scheduler can formalize this as:

$$
C_{\text{placement}}
=
T_{\text{move}}
+
T_{\text{execute}}
+
\lambda E_{\text{move}}
+
\mu E_{\text{execute}}
+
\rho C_{\text{coherence}}
$$

The GPC selects the location with the lowest predicted cost.

---

# 4. Make HBM channels independent execution domains

Each HBM pseudo-channel should receive a compact PIM cluster:

`HBM pseudo-channel
       │
       ▼
┌─────────────────────────────┐
│ PIM command queue           │
│ Address generator           │
│ Bank-aware memory scheduler │
│ 256/512-bit data path       │
│ Binary/INT4/INT8 vector ALU │
│ 4–16 KiB local SRAM         │
│ Partial-sum registers       │
│ ECC and fault monitor       │
└──────────────┬──────────────┘
               │
          reduction network
               │
               ▼
          GPC / TFME`

If there are 16 or 32 independent memory channels, every channel computes a partial result concurrently.

For a matrix-vector operation:

$$
y=Wx
$$

partition $W$ across channels:

$$
W=
\begin{bmatrix}
W_0\\
W_1\\
\vdots\\
W_{C-1}
\end{bmatrix}
$$

Each channel executes:

$$
y_c=W_cx
$$

The results are either concatenated or reduced through the memory-side reduction fabric.

A recent experiment mapped matrix instructions onto Samsung Aquabolt-XL HBM-PIM. It found that current PIM systems need better instruction semantics and software support, but demonstrated elementwise operations, GEMV, and GEMM execution with reduced host involvement. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2604.27808?utm_source=chatgpt.com)

---

# 5. Introduce a dedicated PIM ISA

Normal loads and stores are not enough. PIM needs explicit commands so software knows where an operation executes and who owns the data.

A 256-bit command descriptor could contain:

`PIM_COMMAND {
    opcode
    source_address_0
    source_address_1
    destination_address
    element_count
    matrix_shape
    source_stride
    destination_stride
    data_type
    precision
    reduction_mode
    memory_region
    ownership_epoch
    completion_event
}`

## Core operations

`PIM_COPY
PIM_FILL
PIM_XOR
PIM_XNOR_POPCOUNT
PIM_DOT_I4
PIM_DOT_I8
PIM_GEMV
PIM_GATHER
PIM_SCATTER
PIM_REDUCE_SUM
PIM_REDUCE_MAX
PIM_NORM2
PIM_QUANTIZE
PIM_DEQUANTIZE
PIM_EMBED_LOOKUP
PIM_EMBED_UPDATE
PIM_ADAM_UPDATE
PIM_TOKENIZE`

These become BCIR operations:

`bcir.memory.acquire
bcir.memory.release
bcir.pim.dispatch
bcir.pim.gemv
bcir.pim.reduce
bcir.pim.optimizer
bcir.pim.tokenize
bcir.pim.embedding_gather`

The command ISA should remain stable while the internal PIM microcode can change between memory generations.

---

# 6. Use one address space, but not universal fine-grained coherence

A unified system does not require every cache line to remain coherent during every PIM operation. Full coherence would consume bandwidth and turn the fabric into a traffic jam wearing a lab coat.

Use one virtual address space with explicit ownership states:

`HOST_OWNED
COMPUTE_OWNED
PIM_OWNED
SHARED_READ_ONLY
TRANSITIONING`

Each memory object carries:

`MemoryObject {
    object_id
    virtual_address
    physical_region
    owner
    format
    shape
    precision
    version
    coherence_epoch
    pim_capabilities
    provenance
}`

The execution sequence becomes:

`ACQUIRE region for PIM
        ↓
flush dirty compute caches
        ↓
PIM executes locally
        ↓
increment region version
        ↓
RELEASE region
        ↓
invalidate stale compute copies`

Read-only model weights can remain permanently SHARED_READ_ONLY. Training weights require versioned ownership epochs and double buffering.

A dedicated PIM memory-management unit is not optional. Research on commercial PIM systems found that transfers between conventional DRAM and PIM regions can themselves become a major bottleneck; a hardware copy engine, specialized scheduler, and heterogeneity-aware mapping substantially improved transfer throughput and energy efficiency in the evaluated system. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2409.06204?utm_source=chatgpt.com)

---

# 7. Inference dataflow

## Transformer prefill

Prefill is matrix-heavy and generally has enough arithmetic intensity for the TFME.

`HBM weights
    ↓ streaming
TFME GEMM
    ↓
SRAM-PIM normalization and masks
    ↓
TFME attention and feed-forward
    ↓
HBM KV-cache write`

The PIM engines assist with:

- weight decompression,
- quantization conversion,
- embedding lookup,
- masks,
- KV-cache layout,
- partial reductions.

## Autoregressive decoding

Decode is the natural HBM-PIM target because each token repeatedly streams enormous weight arrays for comparatively little arithmetic.

`Current activation vector
       ↓ broadcast
All HBM PIM channels
       ↓
parallel weight GEMV
       ↓
channel-local partial sums
       ↓
GPC reduction
       ↓
epilogue and next activation`

The weight matrices remain inside HBM. Only activation vectors and reduced outputs cross the main memory boundary.

## KV-cache attention

`Query vector
    ↓ broadcast to HBM channels
PIM: Q · K for local cache blocks
    ↓
global max/sum reduction
    ↓
softmax on SRAM-PIM
    ↓
PIM: probability · V
    ↓
final reduced attention vector`

This lets the system process long contexts without repeatedly dragging the entire KV cache onto the compute die.

---

# 8. Training architecture

Training requires more than inference with writes enabled. It introduces:

- activations,
- gradients,
- master weights,
- optimizer moments,
- accumulation buffers,
- checkpoints,
- collective communication.

The correct division is:

## Compute die

Keep these on the TFME:

- forward GEMM,
- activation-gradient GEMM,
- weight-gradient GEMM,
- high-arithmetic-intensity convolutions,
- large cross-channel reductions.

## HBM-PIM

Move memory-bound stages to PIM:

- gradient accumulation,
- gradient clipping statistics,
- quantization and compression,
- optimizer-state updates,
- weight decay,
- sparse embedding updates,
- checkpoint compression,
- activation packing,
- parameter format conversion.

For Adam-style optimization:

$$
m_t=\beta_1m_{t-1}+(1-\beta_1)g_t
$$

$$
v_t=\beta_2v_{t-1}+(1-\beta_2)g_t^2
$$

$$
w_t=w_{t-1}
-
\eta
\frac{\hat m_t}{\sqrt{\hat v_t}+\epsilon}
$$

The optimizer reads and writes several large arrays but performs relatively little work per byte. A base-die vector engine can update the weights where those arrays already live.

Real-world PIM training experiments indicate that PIM is most effective when the optimizer and datatype are well matched to the hardware; they also show that naïvely increasing the number of PIM devices does not guarantee linear scaling. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2404.07164?utm_source=chatgpt.com)

## Multi-package training

Before data crosses to another package, each local PIM domain should perform:

- partial gradient summation,
- quantization,
- sparsification,
- error-feedback compression.

Then the scale-out fabric carries reduced or compressed gradients rather than raw intermediate tensors.

---

# 9. Token-PIM: make text processing a hardware pipeline

Tokenization should become a first-class hardware subsystem, not a CPU preprocessing afterthought.

## Tokenization pipeline

`Raw text in HBM or external memory
            ↓
UTF-8 validation and byte classification
            ↓
Unicode normalization
            ↓
Whitespace / punctuation / regex-equivalent splitting
            ↓
Parallel vocabulary matching
            ↓
BPE, WordPiece, or Unigram segmentation
            ↓
Token-ID stream
            ↓
Position IDs and attention masks
            ↓
Direct embedding gather
            ↓
BPGC tensor pipeline`

## Which unit handles each stage?

### BPU

The BPU is excellent at:

- classifying hundreds of bytes simultaneously,
- delimiter masks,
- case and character-class comparisons,
- prefix comparison,
- string equality,
- rolling bit masks,
- UTF-8 continuation-byte detection.

### GPC

The GPC handles:

- trie traversal,
- BPE dependency graphs,
- variable-length spans,
- merge scheduling,
- boundary reconciliation,
- output compaction.

### Token-PIM

Memory-side logic stores:

- vocabulary tables,
- merge-rank tables,
- Unicode property tables,
- perfect-hash or cuckoo-hash structures,
- token-to-ID maps,
- embedding rows.

The result can flow directly from token lookup into embedding gathering without returning the token stream to the host.

GPU tokenization research now demonstrates that byte-level BPE can be parallelized, particularly for long inputs, although allocation, merge dependency, boundary handling, and exact compatibility remain important. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv+1](https://arxiv.org/abs/2603.02597?utm_source=chatgpt.com)

For BPGC-UCM, exact compatibility should be mandatory:

$$
\operatorname{Tokenize}_{\text{hardware}}(x)
=
\operatorname{Tokenize}_{\text{reference}}(x)
$$

for every byte sequence in the conformance corpus.

No approximate merge rules in compatibility mode.

---

# 10. Training-data preparation in memory

Token-PIM can perform more than online prompts.

For pretraining, it can execute a continuous data conveyor:

`Compressed dataset
      ↓
decompression
      ↓
text filtering
      ↓
UTF-8 repair or rejection
      ↓
tokenization
      ↓
document-boundary insertion
      ↓
sequence packing
      ↓
attention-mask generation
      ↓
training batch buffers`

Useful memory-side operations include:

- document-length scans,
- sequence packing,
- delimiter detection,
- sample shuffling by descriptor,
- duplicate fingerprints,
- token-frequency histograms,
- curriculum or domain tagging,
- corruption and masking for training objectives.

The tokenizer becomes a **data-ingestion processor**, feeding prepared tensors directly into training.

---

# 11. CXL belongs above HBM, not inside the critical loop

HBM should hold:

- active model layers,
- hot KV cache,
- current activations,
- current gradients,
- hot optimizer partitions.

A coherent external memory tier can hold:

- inactive expert weights,
- cold KV pages,
- datasets,
- checkpoints,
- full optimizer state,
- retrieval indexes,
- large embedding tables.

CXL provides memory and coherence semantics for accelerator and memory expansion use cases, making it appropriate for this capacity tier, but it should not sit in the per-token critical path when HBM locality is available. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Farxiv.org&size=32&drop_404_icon=true)arXiv](https://arxiv.org/abs/2306.11227?utm_source=chatgpt.com)

The memory manager migrates objects between tiers:

`CXL / DDR capacity memory
          ⇅
HBM capacity and HBM-PIM
          ⇅
shared SRAM
          ⇅
tile SRAM-PIM
          ⇅
registers and accumulators`

---

# 12. VHDL integration plan

The HBM PHY, TSV signaling, channel calibration, and vendor DRAM controller should **not** be recreated in ordinary VHDL. Those are foundry or IP-vendor hard macros.

Our VHDL begins at the transaction boundary above the controller.

## New packages

`bpgc_memory_pkg.vhd
bpgc_pim_pkg.vhd
bpgc_token_pkg.vhd
bpgc_coherence_pkg.vhd`

## HBM abstraction

`hbm_interface_adapter.vhd
hbm_channel_router.vhd
hbm_pseudo_channel_model.vhd
hbm_bank_scheduler.vhd
hbm_ecc_monitor.vhd`

## PIM execution

`pim_command_queue.vhd
pim_command_decoder.vhd
pim_address_generator.vhd
pim_binary_lane.vhd
pim_vector_alu.vhd
pim_int8_dot_engine.vhd
pim_gemv_engine.vhd
pim_reduction_tree.vhd
pim_local_sram.vhd
pim_optimizer_engine.vhd
pim_embedding_engine.vhd`

## Tokenization

`token_pim_frontend.vhd
utf8_validator.vhd
byte_classifier.vhd
unicode_normalizer.vhd
pretokenizer.vhd
vocab_hash_lookup.vhd
bpe_pair_rank.vhd
bpe_merge_scheduler.vhd
token_compactor.vhd
sequence_packer.vhd
embedding_gather.vhd`

## Memory control

`unified_memory_manager.vhd
memory_region_table.vhd
ownership_epoch_controller.vhd
pim_mmu.vhd
page_migration_engine.vhd
cache_flush_controller.vhd
fault_and_ecc_manager.vhd`

## Top-level integration

`bpgc_ucm_top.vhd
bpgc_hbm_subsystem.vhd
bpgc_memory_pim_chiplet.vhd
bpgc_ucie_endpoint.vhd
bpgc_cxl_capacity_bridge.vhd`

---

# 13. Recommended interfaces

Use two logically separate fabrics.

## Conventional memory interface

`mem_req_valid
mem_req_ready
mem_req_write
mem_req_address
mem_req_length
mem_req_id
mem_write_data
mem_write_strobe
mem_read_valid
mem_read_data
mem_response`

Suggested data path:

$$
256\text{ or }512\text{ bits per beat}
$$

## PIM command interface

`pim_cmd_valid
pim_cmd_ready
pim_cmd_opcode
pim_cmd_src0
pim_cmd_src1
pim_cmd_dst
pim_cmd_shape
pim_cmd_dtype
pim_cmd_flags
pim_cmd_epoch
pim_completion_valid
pim_completion_id
pim_completion_status`

This prevents ordinary cache traffic from blocking long-running memory-side kernels.

---

# 14. Development roadmap

## Phase 1: Cycle-accurate memory model

Extend the current VHDL system with:

- configurable HBM channels,
- row and bank timing,
- bank conflicts,
- command queues,
- PIM execution latency,
- contention between PIM and conventional loads,
- energy counters.

No physical HBM is required yet.

## Phase 2: HBM-capable FPGA prototype

Use a platform with attached HBM and implement:

- PIM as logic adjacent to the HBM controller,
- GEMV,
- binary dot products,
- reductions,
- embedding lookup,
- exact tokenization.

This validates the architecture without altering DRAM silicon.

## Phase 3: Memory-controller chiplet

Fabricate:

- HBM PHY interfaces,
- PIM command processors,
- reduction network,
- tokenizer,
- unified memory manager.

Use standard HBM stacks.

## Phase 4: Custom HBM base die

Partner with a memory manufacturer to move:

- channel-local PIM,
- address generation,
- decompression,
- optimizer units,
- embedding engines

onto the HBM base die.

## Phase 5: Bank-integrated PIM

Only after the software stack and workloads are proven should arithmetic be pushed deeper into individual DRAM banks.

---

# 15. First benchmark suite

Before expanding the RTL, establish six reference kernels:

| Kernel | Primary target |
| --- | --- |
| Binary GEMV | BPU-PIM |
| INT4 weight GEMV | HBM-PIM |
| KV-cache attention | HBM-PIM plus SRAM-PIM |
| Embedding gather/reduce | HBM-PIM |
| Adam optimizer update | Base-die PIM |
| Exact byte-level BPE | Token-PIM |

Measure:

$$
\text{effective bandwidth}
$$

$$
\text{bytes moved per output token}
$$

$$
\text{energy per token}
$$

$$
\text{PIM occupancy}
$$

$$
\text{HBM bank conflicts}
$$

$$
\text{compute utilization}
$$

$$
\text{tokenizer conformance rate}
$$

$$
\text{training numerical divergence}
$$

---

# Final architecture rule

The deepest memory tier receives the simplest compute. Richer compute appears progressively closer to the BPGC:

$$
\boxed{
\begin{aligned}
\text{DRAM bank PIM} &: \text{logic, dot, gather, partial reduction}\\
\text{HBM base-die PIM} &: \text{GEMV, optimizer, embedding, tokenization}\\
\text{SRAM-PIM} &: \text{attention, masks, local reductions}\\
\text{BPU/TFME} &: \text{GEMM and high-density tensor compute}\\
\text{GPC} &: \text{scheduling, graph flow, ownership, routing}
\end{aligned}
}
$$

That gives us one machine where memory is no longer a silent reservoir. Each tier can think just enough to prevent data from making pointless journeys across the silicon continent. 🧠⚙️

A monthly HBM, PIM, UCIe, and CXL standards watch would be useful while this architecture develops. Want me to set that up?

---

## Message 11 — User

<!-- Message ID: bbb21df4-bf40-4ab8-abc3-a31786777932; Turn index: 5 -->

Lets create a development roadmap for the chips starting from the easiest type to make and building towards more complex components until we have the final architecture. Lets research when to start including silicon photonics and any other emerging technologies that TSMC and other similar companies are starting to manufacture so we dont have to do research in exotic new chip designs. Lets also do another looking into the viability of incorporating memristors and if they should be included in the roadmap or skipped entirely. Lets create the blueprint proposals for the generational development roadmap

---

## Message 12 — Assistant

<!-- Message ID: b2906a9a-f76f-4850-8ff7-12b56c52538d; Turn index: 5 -->

# BPGC generational chip roadmap

The cleanest strategy is:

$$
\boxed{
\text{digital CMOS}
\rightarrow
\text{SRAM compute}
\rightarrow
\text{chiplets}
\rightarrow
\text{HBM}
\rightarrow
\text{PIM}
\rightarrow
\text{3D integration}
\rightarrow
\text{optical I/O}
}
$$

**Analog memristor computation should not sit on the critical path.** ReRAM should remain an optional embedded-memory branch until qualified foundry IP proves that it offers a system-level advantage.

The central rule is simple:

> Each generation introduces only one major manufacturing risk while retaining the software, ISA, RTL, and compiler developed for every earlier generation.

This creates a staircase rather than a technological cliff. 🧱⚙️

---

# 1. Master roadmap

The dates below are recommended program windows, not foundry guarantees. A serious ASIC generation normally requires approximately 18–30 months from specification through validated silicon, with some work overlapping.

| Generation | Proposed product | Approximate window | Manufacturing leap | Primary purpose |
| --- | --- | --- | --- | --- |
| G0 | **BPGC-Proto** | 2026–2027 | FPGA and MPW test silicon | Validate architecture and software |
| G1 | **BPGC-E28** | 2027–2029 | Monolithic 28 nm CMOS | Cheap commercial inference |
| G2 | **BPGC-D22** | 2029–2030 | 22 nm FD-SOI or 16/12 nm, digital SRAM-PIM | Higher efficiency and density |
| G3 | **BPGC-CX** | 2030–2032 | UCIe chiplets and advanced packaging | Scalable inference and training-lite |
| G4 | **BPGC-H** | 2032–2034 | HBM3E/HBM4, controller-side PIM | Large models and memory bandwidth |
| G5 | **BPGC-3D** | 2034–2036 | Hybrid bonding, stacked SRAM, HBM base-die PIM | Unified compute-memory package |
| G6 | **BPGC-O** | 2035–2037 | Silicon-photonic I/O chiplets | Multi-package AI scaling |
| G7 | **BPGC-UCM** | 2037+ | Unified optical, HBM, PIM, CXL system | Final inference/training architecture |

Generations should overlap. For example, the optical-I/O team could begin interface design during G3 while the first physical optical product does not appear until G6.

---

# 2. G0: BPGC-Proto

## Objective

Prove that the BPU, GPC, Tensor Fusion Matrix Engine, tokenizer, graph pipeline, compiler, and memory scheduler produce useful performance before spending money on masks.

`Host CPU
   │
PCIe
   │
FPGA
├── GPC controller
├── BPU array
├── TFME
├── tokenizer
├── graph gather/reduce/scatter
├── DDR/HBM controller
└── performance counters`

## Hardware

Use:

- a conventional FPGA board,
- a second HBM-equipped FPGA platform for memory experiments,
- the existing VHDL architecture,
- vendor SRAM and DSP blocks only where needed,
- PCIe and DDR/HBM controller IP.

No custom process technology belongs here.

## Deliverables

G0 must establish:

1. The command ISA and BCIR lowering path.
2. Correct B1, ternary, INT2, INT4, W4A8, and INT8 operation.
3. Exact tokenization compatibility.
4. Graph gather/reduce/scatter correctness.
5. Cycle-accurate memory and NoC models.
6. Arithmetic utilization, bandwidth, and power proxies.
7. A reproducible benchmark suite.
8. A software simulator that remains compatible with every later generation.

## Exit gate

Proceed only when representative workloads sustain at least 60–70% of modeled BPU/TFME utilization. If they do not, the problem is probably scheduling or memory movement rather than transistor performance.

---

# 3. G1: BPGC-E28, the first commercial chip

## Product definition

A low-cost, monolithic inference accelerator manufactured in a completely standard 28 nm CMOS process.

`┌──────────────────────────────────────────┐
│ Management core                          │
│ PCIe / USB / Ethernet / host interface   │
├──────────────────────────────────────────┤
│ 8–16 GPC tiles                           │
│ 1-bit / ternary / INT2 / INT4 BPU        │
│ Shared INT8 compatibility array          │
│ Vector normalization unit                │
├──────────────────────────────────────────┤
│ 4–16 MiB compiled SRAM                   │
│ LPDDR5 / DDR5 / optional GDDR interface  │
└──────────────────────────────────────────┘`

## Manufacturing policy

Everything should come from the foundry PDK:

- planar or foundry-standard 28 nm transistors,
- compiled SRAM,
- standard I/O libraries,
- PLLs,
- PCIe PHY,
- DDR or LPDDR PHY,
- conventional flip-chip or wire-bond package.

Do **not** use:

- custom vertical GAA,
- HBM,
- hybrid bonding,
- silicon photonics,
- analog compute,
- custom memory materials,
- custom SRAM bit cells.

## Workloads

G1 should target:

- edge vision,
- speech and audio,
- recommendation inference,
- smaller quantized transformers,
- binary and ternary networks,
- graph analytics,
- Hamming retrieval,
- hardware tokenization,
- low-cost AI appliances.

## Why G1 matters

It validates the business and software architecture. The first chip does not need to be the ultimate machine. It must be:

- manufacturable,
- debuggable,
- affordable,
- programmable,
- useful enough to sell.

A successful 28 nm accelerator becomes the architectural seed from which every later chiplet grows.

---

# 4. G2: BPGC-D22, digital compute-near-memory

G2 moves from conventional memory plus arithmetic toward **fully digital SRAM compute**, without crossing into analog memory.

## Candidate process

Use one of:

- 22 nm FD-SOI,
- 16/12 nm FinFET,
- a mature foundry-specific low-power process.

FD-SOI is particularly attractive for lower leakage and voltage adaptation, while 16/12 nm FinFET may offer denser logic and more familiar high-performance IP. The selection should depend on SRAM compiler availability, PHY licensing, wafer cost, and expected volume, not node-name theatre.

## New components

`SRAM bank
├── ordinary read/write port
├── isolated compute-read port
├── XNOR / AND / mask plane
├── column reduction
└── ECC`

Add:

- 8T SRAM compute-read cells or foundry-supported SRAM-CIM macros,
- bank-local XNOR and POPCOUNT,
- structured-sparsity masks,
- local reduction trees,
- tokenizer vocabulary caches,
- embedding lookup acceleration,
- dual or triple voltage domains,
- stronger clock and power gating.

## Memory

Remain with relatively affordable memory:

- LPDDR,
- DDR,
- GDDR,
- large on-chip SRAM.

HBM is still premature unless the product price can absorb advanced packaging.

## Training capability

G2 can support:

- quantization-aware training experiments,
- fine-tuning of small models,
- gradient accumulation,
- INT8/INT16 parameter updates.

It should not be positioned as a large-model training processor.

---

# 5. Optional G2-N: embedded nonvolatile memory experiment

This is the correct point to investigate ReRAM or eMRAM, but only as **embedded storage**, not as the main arithmetic fabric.

Potential uses:

- secure boot ROM replacement,
- device identity and keys,
- microcode,
- fault maps,
- quantization tables,
- tokenizer vocabulary,
- model manifests,
- small, rarely changed weight blocks,
- instant-on edge models.

Vendor-reported commercial ReRAM products now exist for mature 130 nm platforms, and development has extended toward GlobalFoundries’ 22FDX ecosystem. Weebit’s product material identifies BEOL integration and edge-AI weight storage as target applications; it also quotes GlobalFoundries stating that embedded flash becomes difficult to scale below 28 nm and that embedded ReRAM is one candidate replacement. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.weebit-nano.com&size=32&drop_404_icon=true)Weebit+1](https://www.weebit-nano.com/)

## Test chip

Fabricate a tiny independent vehicle containing:

`ReRAM macro
├── boot/config region
├── model-weight region
├── ECC
├── endurance counters
├── temperature monitors
└── SRAM shadow buffer`

Do not connect it directly to the core timing path. The processor must operate correctly if the ReRAM block is disabled.

## Graduation criteria

Embedded ReRAM enters a commercial generation only after demonstrating:

- qualified foundry PDK and memory compiler,
- retention across intended temperature,
- endurance appropriate to the workload,
- ECC behavior,
- wafer-level yield,
- known-good-die testability,
- less total system cost than external flash,
- no disruption to logic or SRAM yield.

---

# 6. G3: BPGC-CX, the chiplet transition

This is the most important architectural generation after first silicon.

Instead of manufacturing everything on one expensive node, separate the machine into dies optimized for different jobs.

`┌────────────────────────────────────────────────────────┐
│ Organic substrate or 2.5D package                      │
│                                                        │
│  Compute chiplet         Compute chiplet               │
│  5/7/12 nm               5/7/12 nm                     │
│       │ UCIe                  │ UCIe                    │
│       └──────────┬────────────┘                         │
│                  │                                     │
│        I/O and memory chiplet                           │
│        22/28 nm                                         │
│        PCIe, CXL, DDR/GDDR, security, management        │
└────────────────────────────────────────────────────────┘`

UCIe defines a package-level die-to-die physical layer, protocol stack, software model, and compliance system. The consortium explicitly positions it for combining chiplets from different processes and vendors, lowering portfolio cost and supporting standardized testing. UCIe 2.0 also adds multi-chiplet manageability and 3D/hybrid-bonding support. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.uciexpress.org&size=32&drop_404_icon=true)UCIe Consortium+1](https://www.uciexpress.org/specifications)

## Why chiplets come before HBM and photonics

Chiplets solve several problems immediately:

- yield,
- die-size limits,
- process-node specialization,
- reusable I/O,
- scalable product families,
- easier experimental integration,
- independent replacement of compute and memory dies.

TSMC’s 3DFabric strategy similarly separates technologies such as CoWoS and SoIC, allowing logic, memory, and I/O blocks to be built on different nodes and then integrated. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2F3dfabric.tsmc.com&size=32&drop_404_icon=true)3DFabric](https://3dfabric.tsmc.com/english/dedicatedFoundry/technology/3DFabric.htm)

## G3 product family

Create three packages from common chiplets:

- **CX-E:** one compute die, low-cost memory.
- **CX-S:** two to four compute dies for server inference.
- **CX-T:** more accumulators and higher-precision logic for fine-tuning.

## Photonics preparation begins here

G3 should not contain photonics, but its interfaces should be photonics-ready:

- streaming-friendly UCIe ports,
- detachable I/O-chiplet slots,
- optical-link management registers,
- forward-error-correction hooks,
- link telemetry,
- lane remapping,
- clean separation between compute and SerDes protocols.

This avoids redesigning the compute die when optical I/O arrives later.

---

# 7. G4: BPGC-H, HBM and controller-side PIM

G4 is the first true large-model architecture.

## Package

`                    HBM stack
                        │
Compute chiplets ─ Memory/I/O chiplet ─ HBM stack
                        │
                    HBM stack`

Use:

- HBM3E, HBM4, or the most economically qualified successor,
- a 2.5D interposer or equivalent advanced package,
- dedicated memory-controller chiplet,
- local BPU/GPC compute dies,
- UCIe links between dies.

HBM4 products began shipping from multiple suppliers in 2026, but supply, pricing, and packaging remain major business constraints. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)Reuters](https://www.reuters.com/technology/samsung-electronics-says-it-has-shipped-hbm4-chips-customers-2026-02-12/?utm_source=chatgpt.com)

A very recent development worth watching is **SPHBM4**, a newly reported JEDEC standard intended to retain HBM-class behavior while using a narrower 512-bit interface compatible with less expensive organic substrates. Because the specification is only just emerging in July 2026, it should be treated as a G4 option under evaluation, not as a committed dependency. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware](https://www.tomshardware.com/pc-components/dram/jedec-releases-new-sphbm4-standard-to-slash-ai-memory-costs-narrow-512-bit-interface-enables-dropping-expensive-interposers-for-organic-substrates?utm_source=chatgpt.com)

## PIM stage one: compute beside memory

Do not modify the DRAM stack yet.

Place PIM engines inside the memory-controller chiplet:

`HBM channel
   │
Channel scheduler
   │
PIM engine
├── XOR/XNOR/POPCOUNT
├── INT4/INT8 vector operations
├── quantize/dequantize
├── decompression
├── embedding gather
├── KV-cache scan
└── partial reduction`

This can be manufactured as standard logic and tested independently from the HBM.

## Work allocation

### Compute dies

- dense GEMM,
- convolution,
- attention matrix products,
- forward and backward tensor operations,
- high-arithmetic-intensity training.

### Memory-side PIM

- batch-1 GEMV,
- embedding lookup,
- KV-cache scanning,
- decompression,
- sparse gathering,
- reductions,
- quantization,
- optimizer-state preparation.

Samsung has demonstrated the concept of placing DRAM-oriented compute engines near individual HBM banks, reporting more than double performance and over 70% lower energy on its evaluated workloads. These are vendor-specific results, but they validate the architectural direction of reducing memory traffic. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fnews.samsung.com&size=32&drop_404_icon=true)Samsung Global Newsroom](https://news.samsung.com/global/samsung-develops-industrys-first-high-bandwidth-memory-with-ai-processing-power)

## Tokenization

G4 should integrate the full Token-PIM path:

`text in HBM
   ↓
UTF-8 classification
   ↓
normalization / pretokenization
   ↓
BPE or WordPiece lookup
   ↓
token IDs
   ↓
embedding gather
   ↓
tensor engine`

Token data, vocabulary tables, and embeddings can remain in the memory subsystem without bouncing through a host CPU.

---

# 8. G5: BPGC-3D, vertical integration without custom transistors

This generation adopts vertical packaging, not a custom vertical-transistor process.

## Architecture

`              Stacked SRAM/cache die
                       │
                 Hybrid bonding
                       │
             BPU/TFME compute die
                       │
                 UCIe / interposer
                       │
       HBM stacks + memory/PIM base die`

UCIe 2.0 explicitly supports 3D integration and hybrid-bonding pitches from roughly 10–25 μm down to 1 μm or less, making it a standards-based path toward fine-pitch die stacking. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.uciexpress.org&size=32&drop_404_icon=true)UCIe Consortium+1](https://www.uciexpress.org/specifications)

## New technologies

- stacked SRAM or cache die,
- logic-on-memory or memory-on-logic,
- hybrid bonding,
- thermal-aware tile placement,
- base-die PIM,
- distributed power and thermal sensors,
- memory-object ownership hardware,
- redundancy across chiplets.

## Advanced transistor node

The compute die may now move to foundry-supplied:

- GAA nanosheet technology,
- backside-power technology,
- or the equivalent available from the selected foundry.

Do not design a private V-GAA process.

TSMC’s N2 generation is its first production GAA nanosheet platform, while public 2026 roadmap reporting places the company’s A16 backside-power generation around 2027. By G5, these should be evaluated as commercially supplied options rather than research projects. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware+1](https://www.tomshardware.com/tech-industry/semiconductors/tsmc-unveils-process-technology-roadmap-through-2029-a12-a13-n2u-announced-a16-slips-to-2027?utm_source=chatgpt.com)

Only the compute die needs this expensive node. Memory control, security, I/O, and tokenizer logic can remain on cheaper mature processes.

## Training

G5 becomes the first fully credible training system:

- BF16/FP16 compatibility paths,
- FP8 and INT8 tensor modes,
- gradient accumulation,
- optimizer engines near HBM,
- multi-die collective reduction,
- checkpoint compression,
- activation recomputation control,
- error-feedback gradient compression.

---

# 9. When should silicon photonics enter?

## Architectural preparation: G3

Define the interfaces and telemetry early.

## Prototype optical link: G4 or G5

Use a detachable optical engine connected to an I/O chiplet. Keep lasers and fiber attachment outside the main compute package where possible.

## Commercial near-package optics: G5

Use near-packaged optics for the largest server system if copper links require excessive SerDes power or retiming.

## Co-packaged optics: G6

Integrate a photonic chiplet in the accelerator package only when electrical I/O has become a measured system limit.

GlobalFoundries already describes production-proven silicon-photonics platforms supporting pluggable, near-packaged, and co-packaged optics, with 300 mm and 200 mm manufacturing, qualified PDKs, and packaging support. Its current platform is proven up to 200 Gb/s per wavelength, with a 400 Gb/s-per-wavelength generation in early access. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fgf.com&size=32&drop_404_icon=true)GlobalFoundries+1](https://gf.com/technology-solutions/silicon-photonics/)

An ecosystem demonstration around TSMC’s COUPE platform appeared in late 2025, combining an optical engine, electrical interface chiplet, detachable fiber connection, and UCIe protocol connection. This is a useful signal that optical I/O is becoming accessible through foundry and packaging ecosystems rather than requiring every accelerator company to invent its own photonic process. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware](https://www.tomshardware.com/tech-industry/semiconductors/industrys-first-tsmc-coupe-based-optical-connectivity-solution-for-next-gen-ai-chips-displayed-alchip-and-ayar-labs-show-future-silicon-photonics-device?utm_source=chatgpt.com)

## Recommended admission triggers

Introduce packaged optical I/O when at least two of these conditions are met:

- off-package electrical I/O consumes more than 15% of package power,
- aggregate package bandwidth requirement exceeds approximately 50–100 Tb/s,
- substrate shoreline limits the number of electrical lanes,
- copper reach requires multiple retimers,
- multi-rack scaling is a core workload requirement,
- optical packaging and known-good-die yield are commercially acceptable.

These are internal engineering gates, not claims about a universal industry threshold.

## What photonics should do

Use photons for:

- chip-to-chip links,
- package-to-package links,
- rack scale-up,
- rack scale-out,
- memory-fabric extension.

Do not make the initial roadmap depend on:

- optical matrix multiplication,
- optical SRAM,
- phase-change photonic weights,
- on-chip laser research,
- photonic nonlinear activation.

Those remain research programs rather than necessary product technologies.

---

# 10. G6: BPGC-O, optical unified packages

## Package blueprint

`┌───────────────────────────────────────────────────────────┐
│ BPGC-O package                                            │
│                                                           │
│ Compute chiplets       3D SRAM stack                      │
│      │                    │                               │
│      └──── UCIe fabric ───┤                               │
│                           │                               │
│ HBM stacks ─ Memory/PIM chiplet                           │
│                           │                               │
│                 Optical I/O chiplets                      │
│                 CWDM / DWDM links                         │
└───────────────────────────────────────────────────────────┘`

## Optical operation

Electrical links remain appropriate inside very short package distances.

Optical links take over:

- between accelerator packages,
- between accelerator trays,
- between racks,
- between compute and disaggregated memory pools.

The GPC treats optical links as another routed fabric:

`local SRAM
HBM
remote HBM
CXL memory
remote accelerator`

Each location becomes a registry-addressable resource with:

- latency,
- bandwidth,
- energy,
- ownership,
- version,
- reliability,
- current congestion.

---

# 11. G7: BPGC-UCM, the final architecture

`                           Optical fabric
                    ┌──────────┴──────────┐
                    │                     │
              BPGC package          BPGC package
                    │                     │
      ┌─────────────┴─────────────┐       │
      │                           │       │
Compute chiplets             HBM/PIM      │
BPU + GPC + TFME             subsystem    │
      │                           │       │
3D SRAM-PIM              Token/embedding  │
      │                    optimizer PIM  │
      └──────── UCIe fabric ──────┘       │
                    │                     │
              CXL capacity memory / storage`

## Final execution hierarchy

$$
\begin{aligned}
\text{Registers} &:\text{ immediate arithmetic}\\
\text{Tile SRAM} &:\text{ tensor reuse and local PIM}\\
\text{Stacked SRAM} &:\text{ shared activations and KV cache}\\
\text{HBM} &:\text{ weights, gradients, optimizer state}\\
\text{HBM-PIM} &:\text{ memory-bound tensor and graph operations}\\
\text{CXL memory} &:\text{ cold models, datasets and checkpoints}\\
\text{Optical fabric} &:\text{ package and rack-scale transport}
\end{aligned}
$$

## Software view

Software sees one registry-oriented machine:

`MemoryObject {
    object_id
    location
    owner
    precision
    shape
    sparsity
    version
    permitted_execution_sites
    bandwidth_cost
    movement_cost
    confidence
}`

The scheduler chooses among:

- BPU,
- TFME,
- SRAM-PIM,
- HBM-controller PIM,
- HBM base-die PIM,
- remote accelerator,
- CPU fallback.

---

# 12. Final memristor decision

## The original design was technically cautious

The GAADMSF architecture used a differential 2T2R arrangement, program-and-verify loops, GAA arbitration, common-mode rejection, and a CMOS fallback. [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf] [2025-12-16-Design Specification_ GAADMSF Primitive Cell.pdf]

It also required:

- resistance programming,
- mandatory verification,
- retries,
- confidence maps,
- variance estimates,
- drift counters,
- scrubbing,
- wear-aware scheduling,
- ADC or comparator sensing,
- routing around degraded cells. [GAADMSF Primitive Cell - Hybrid GAA+Memristor Architecture.pdf] [GAADMSF Primitive Cell - Hybrid GAA+Memristor Architecture.pdf]

Those mechanisms are intelligent, but they reveal the hidden systems tax. The previous RTL itself contains verify-margin, variance, confidence, drift, and failure-handling logic that a digital SRAM architecture does not need. [gaadmsf_verilog.txt] [gaadmsf_verilog.txt]

## Verdict by application

| Memristor/ReRAM use | Roadmap decision |
| --- | --- |
| Secure boot/configuration | **Include as optional eNVM** |
| Model manifest and tokenizer tables | **Include as optional eNVM** |
| Rarely updated edge weights | **Evaluate** |
| Persistent routing configuration | **Evaluate** |
| Digital read-only inference memory | **Possible after qualification** |
| Analog matrix multiplication | **Skip from main roadmap** |
| Frequently updated training weights | **Skip** |
| Optimizer state | **Skip** |
| Primary HBM replacement | **Skip** |
| Essential dependency for BPGC | **Never** |

## Analog-compute graduation test

A memristor compute chiplet should be reconsidered only if a foundry or IP partner supplies:

1. A production-qualified PDK.
2. Characterized arrays across process, voltage, and temperature.
3. Production ADC/DAC and sense-amplifier IP.
4. Endurance and retention guarantees.
5. Error models accepted by the compiler.
6. Known-good-die test procedures.
7. At least a **2× end-to-end** energy or throughput improvement after calibration, conversion, and data movement.
8. Acceptable model accuracy without continual retraining.
9. A product workload that cannot be served adequately by SRAM-PIM or HBM-PIM.

Until all nine conditions are met, analog memristor compute remains an isolated research branch.

---

# 13. Technology admission matrix

| Technology | Architecture preparation | First prototype | Product inclusion |
| --- | --- | --- | --- |
| Standard digital CMOS | G0 | G0 | G1 |
| Digital SRAM-PIM | G1 | G1/G2 | G2 |
| Embedded ReRAM/eMRAM | G1 | G2-N | G3 optional |
| UCIe chiplets | G2 | G3 | G3 |
| 2.5D advanced packaging | G2 | G3 | G3/G4 |
| HBM | G3 | G4 | G4 |
| Controller-side PIM | G3 | G4 | G4 |
| Hybrid bonding/3D SRAM | G4 | G5 | G5 |
| HBM base-die PIM | G4 | G5 | G5/G6 |
| Foundry GAA nanosheets | G4 | G5 | G5 |
| Backside power delivery | G4/G5 | G5 | G5/G6 |
| Photonic interface hooks | G3 | G4 | All later dies |
| Near-package optics | G4 | G5 | G5 optional |
| Co-packaged optical I/O | G5 | G6 | G6 |
| Analog memristor compute | Independent research | G4+ test chip | Only after graduation |
| Custom V-GAA process | None | None | **Excluded** |
| Photonic tensor compute | Research only | None planned | **Excluded** |

---

# 14. Final strategic blueprint

## Main production branch

`BPGC-Proto
    ↓
BPGC-E28
Monolithic digital inference
    ↓
BPGC-D22
Digital SRAM compute
    ↓
BPGC-CX
UCIe chiplets
    ↓
BPGC-H
HBM + controller PIM
    ↓
BPGC-3D
Hybrid bonding + stacked SRAM + base-die PIM
    ↓
BPGC-O
Silicon-photonic I/O
    ↓
BPGC-UCM
Unified compute-memory-optical system`

## Optional nonvolatile branch

`Qualified embedded ReRAM/eMRAM
        ↓
boot, configuration, security
        ↓
tokenizer and persistent metadata
        ↓
rarely changed edge-model weights`

## Quarantined research branch

`Analog memristor crossbar
        ↓
independent test vehicle
        ↓
system-level comparison against SRAM-PIM
        ↓
graduate only if >2× real advantage`

The roadmap’s spine should therefore be:

> **CMOS first, digital memory compute second, chiplets third, HBM and PIM fourth, 3D integration fifth, photons sixth.**

Memristors are not erased from the research map, but they no longer get to hold the main architecture hostage. The final system is built from technologies foundries can manufacture, package, test, and replace generation by generation, while the exotic dragons remain behind a very sturdy stage gate. 🐉🔒

A quarterly foundry-technology watch should track HBM, SPHBM, UCIe, advanced packaging, photonics, and qualified ReRAM so the roadmap can advance or delay individual technologies as manufacturing reality changes.

[

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![JEDEC releases new SPHBM4 standard to slash AI memory costs - Narrow 512-bit interface enables dropping expensive interposers for organic substrates](https://images.openai.com/static-rsc-1/SEOrCj_b8TGnkgJD9SImr12INgMqIOnspUNPAWOF5fwkfQisX0takWlCQsrkdSZ3H25TzYg7NGQBUGhTHdVryOUEpmk-qypKujVSF9gUBINmhPsED2g8FsJxrJ3ukWjs)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware

JEDEC releases new SPHBM4 standard to slash AI memory costs - Narrow 512-bit interface enables dropping expensive interposers for organic substrates

Jul 8, 2026

](https://www.tomshardware.com/pc-components/dram/jedec-releases-new-sphbm4-standard-to-slash-ai-memory-costs-narrow-512-bit-interface-enables-dropping-expensive-interposers-for-organic-substrates?utm_source=chatgpt.com)[

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![TSMC unveils process technology roadmap through 2029 - A12, A13, N2U announced, A16 slips to 2027](https://images.openai.com/static-rsc-1/fAE3kICxxbul7IYcEzp303OU-Hacj_eFQuJXxBbz4KMSmaA1sWrPe6Zeg0KkXCAxWCvb69WMhf6fv5-kwtAnIDta-XzUCQpKCCuYKym0tiI4aSBSc5Y4y63pEfmSgF0i)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware

TSMC unveils process technology roadmap through 2029 - A12, A13, N2U announced, A16 slips to 2027

Apr 22, 2026

](https://www.tomshardware.com/tech-industry/semiconductors/tsmc-unveils-process-technology-roadmap-through-2029-a12-a13-n2u-announced-a16-slips-to-2027?utm_source=chatgpt.com)[

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)

![Industry's first TSMC COUPE-based optical connectivity solution for next-gen AI chips displayed - Alchip and Ayar Labs show future silicon photonics device](https://images.openai.com/static-rsc-1/K5Dp2acpQPWZAZtm1p6BZvON_kW10Dl7du6hnqSmNmeJ51AO2d4lXAxho4_L5QtuxJNQxJArKAi4Y5DbJoCBzEpna3NrfJEamVsvvlD2a0rxs_yJbOlkOvU13JPb6TfF)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.tomshardware.com&size=32&drop_404_icon=true)Tom's Hardware

Industry's first TSMC COUPE-based optical connectivity solution for next-gen AI chips displayed - Alchip and Ayar Labs show future silicon photonics device

Dec 1, 2025

](https://www.tomshardware.com/tech-industry/semiconductors/industrys-first-tsmc-coupe-based-optical-connectivity-solution-for-next-gen-ai-chips-displayed-alchip-and-ayar-labs-show-future-silicon-photonics-device?utm_source=chatgpt.com)[

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)

![Samsung ships latest HBM4 chips to catch-up in AI race](https://images.openai.com/static-rsc-1/kMvT0TUo3JMoXIsCK4YqC6naMEZLC0qtZqcwSiPqDITLQDMjnzA82m8r3Dck-tGw6ViIR1lbU4yyEhTxILxRLaXsW6ILzE2jpK78txv0kVFOkP7ENI0INRo8-BmHrHldnaNqpZbSwRsXsm5iiYLhs8CDlZQ3daF5qPkgTc51c7DBhAe8IW_oFSVRLr9tDdq-FPlqvtULLJdzk-HfoqGaGvUpbslTWLfECGKHkLYEZw8wR1Rlxr7P4bc-MWj3BBL4faC491-SlVlxcDPZmg-JXw)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)Reuters

Samsung ships latest HBM4 chips to catch-up in AI race

Feb 12, 2026

](https://www.reuters.com/technology/samsung-electronics-says-it-has-shipped-hbm4-chips-customers-2026-02-12/?utm_source=chatgpt.com)[

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)

![GlobalFoundries boosts investment plans to $16 billion, with research focus](https://www.reuters.com/resizer/o3NXq4EuTCAP76iUkgf9N9NqoQA=/cloudfront-us-east-2.images.arcpublishing.com/reuters/Z62JBUVHKVJMHOTT5WVUQZPFEQ.jpg)

![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fwww.reuters.com&size=32&drop_404_icon=true)Reuters

GlobalFoundries boosts investment plans to $16 billion, with research focus

Jun 4, 2025

](https://www.reuters.com/world/asia-pacific/globalfoundries-boosts-investment-plans-16-billion-with-research-focus-2025-06-04/?utm_source=chatgpt.com)

---

## Message 13 — User

<!-- Message ID: bbb2152f-e0ba-4cde-9cb3-4d0f3e0bb0db; Turn index: 6 -->

Provide the full VHDL implementation for the latest proposed chips

---

## Message 14 — Assistant

<!-- Message ID: a7831c3c-816f-4951-ae8b-4f81fe415852; Turn index: 6 -->

# BPGC-UCM VHDL implementation package

The complete **VHDL-2008 architectural reference RTL** is ready. It implements the full generational progression from the inexpensive monolithic inference chip through the unified compute, HBM-PIM, tokenization, training, chiplet, optical-I/O, and CXL architecture. ⚙️🧠

BPGC_UCM_VHDL_Reference_RTL_v0.5.0.zip[Download BPGC-UCM VHDL Reference RTL v0.5.0](sandbox:/mnt/data/BPGC_UCM_VHDL_Reference_RTL_v0.5.0.zip)

## Implemented generations

| Profile | Architecture |
|---|---|
| **G1 BPGC-E28** | BPU, GPC, TFME, graph execution, INT8 fallback |
| **G2 BPGC-D22** | G1 plus digital SRAM-PIM |
| **G3 BPGC-CX** | G2 plus logical UCIe chiplet transport |
| **G4 BPGC-H** | HBM routing, controller-side PIM, tokenization and embedding |
| **G5 BPGC-3D** | Stacked-SRAM and hybrid-bonding interface profile |
| **G6 BPGC-O** | Digital silicon-photonic link adapter |
| **G7 BPGC-UCM** | Complete system with CXL capacity memory and optional embedded NVM |

The selected product is controlled by the `GENERATION` generic in `bpgc_ucm_top`.

## Major RTL subsystems

The package contains **63 RTL source files and 57 synthesizable entities**, including:

- Binary, ternary, INT2, INT4, W2A8 and INT8 computation
- Tensor Fusion Matrix Engine and persistent accumulator fabric
- Tensor epilogue, activation and normalization assistance
- CSR graph traversal, gather, segmented reduction and scatter
- Programmable GPC microsequencer and command processors
- Memory-region registry, ownership epochs and migration
- Compute, PIM and tokenizer memory arbitration
- Multi-channel HBM address routing and behavioral memory models
- SRAM-PIM banks
- Binary, INT4 and INT8 PIM engines
- PIM GEMV, optimizer, quantization and embedding engines
- UTF-8 validation, byte classification and programmable BPE
- Token-to-embedding gathering and sequence packing
- Logical UCIe endpoint
- Optical lane adapter
- CXL capacity-memory transaction bridge
- Optional embedded ReRAM, eMRAM or eFuse controller abstraction
- Final unified `bpgc_ucm_top`

## Key files

bpgc_all_in_one.vhd[Combined synthesizable RTL](sandbox:/mnt/data/bpgc_ucm_vhdl/project/dist/bpgc_all_in_one.vhd)

bpgc_testbenches_all.vhd[Combined VHDL testbenches](sandbox:/mnt/data/bpgc_ucm_vhdl/project/dist/bpgc_testbenches_all.vhd)

:chatgpt-content-reference{index="3"}[Project README](sandbox:/mnt/data/bpgc_ucm_vhdl/project/README.md)

:chatgpt-content-reference{index="4"}[Full architecture specification](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/ARCHITECTURE.md)

:chatgpt-content-reference{index="5"}[Generational profile definitions](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/GENERATION_PROFILES.md)

:chatgpt-content-reference{index="6"}[PIM instruction-set specification](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/PIM_ISA.md)

:chatgpt-content-reference{index="7"}[Tokenizer architecture](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/TOKENIZER_ARCHITECTURE.md)

:chatgpt-content-reference{index="8"}[Hard-IP integration boundaries](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/HARD_IP_BOUNDARIES.md)

:chatgpt-content-reference{index="9"}[Implementation and verification status](sandbox:/mnt/data/bpgc_ucm_vhdl/project/docs/IMPLEMENTATION_STATUS.md)

BPGC_UCM_VHDL_Reference_RTL_v0.5.0.zip.sha256[SHA-256 checksum](sandbox:/mnt/data/BPGC_UCM_VHDL_Reference_RTL_v0.5.0.zip.sha256)

```text
fda849d821bc1ccf582e5e2c6dd9815b84f7f1b3066e6ed8dd681aa0ddb1770b
```

## Validation completed

- Structural lint passed across **73 RTL and simulation files**, covering **67 entities**
- Every RTL and testbench file parsed successfully in VHDL syntax-only mode
- Arithmetic and architectural golden-model tests passed
- All seven product profiles passed static integration checks
- Combined distribution files generated successfully
- ZIP integrity test passed with no archive errors

A full elaborating VHDL compiler was not available in the generation environment, so GHDL, NVC, Questa, or equivalent compilation and waveform simulation remain required. The package is a comprehensive synthesizable-reference baseline, but it is not yet tapeout-qualified.

The HBM PHY, UCIe electrical PHY, CXL controller, foundry SRAM arrays, embedded nonvolatile-memory macro, optical modulators, SerDes, laser interface, and package-calibration logic are represented by clean digital transaction boundaries. Those elements must be replaced with licensed foundry or vendor hard IP during ASIC integration.

---

## Message 15 — User

<!-- Message ID: bbb219a3-0e4a-4956-998f-fc0814ebb342; Turn index: 7 -->

Can the chips be built as a standard PCIe Add-In Card (like a sound card or a second GPU) or connect via CXL (Compute Express Link)?

---

## Message 16 — Assistant

<!-- Message ID: 30c48a6e-f1fe-49cd-b5a7-71f6a75de787; Turn index: 7 -->

## Yes. The BPGC family can be built as a normal PCIe add-in card, and later versions can use CXL over the same basic physical card concept

The best commercial sequence is:

$$
\boxed{
\text{PCIe accelerator card}
\rightarrow
\text{PCIe card with local HBM}
\rightarrow
\text{dual-mode PCIe/CXL Type 2 card}
\rightarrow
\text{CXL accelerator plus pooled-memory system}
}
$$

A BPGC card could plug into a workstation or server almost exactly like a second GPU, FPGA accelerator, network processor, or professional sound card. PCI-SIG’s current Card Electromechanical specification explicitly accommodates compute accelerators and machine-learning cards, with standardized power classes ranging from 10 W through 675 W. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fpcisig.com&size=32&drop_404_icon=true)PCI-SIG+1](https://pcisig.com/PCIExpress/Spec/CEM/CardElectromechanical_6.0.1?utm_source=chatgpt.com)

---

# 1. Simplest product: a PCIe BPGC add-in card

`Host CPU
   │
PCIe x16 slot
   │
┌──▼──────────────────────────────────────────┐
│ BPGC PCIe Add-In Card                       │
│                                             │
│ PCIe PHY and endpoint                       │
│ DMA and command queues                      │
│ BPGC controller                             │
│ BPU + GPC + TFME tiles                      │
│ Local SRAM                                  │
│ LPDDR / GDDR / optional HBM                 │
│ Firmware and board-management controller    │
└─────────────────────────────────────────────┘`

To the operating system, the card would appear as a PCIe accelerator device with:

- memory-mapped control registers,
- command and completion queues,
- DMA engines,
- interrupts,
- device-local memory,
- telemetry and error registers,
- one or more compute contexts.

The host would submit inference, tokenization, graph, tensor, or PIM commands through a driver. The card would execute them independently and return results through DMA.

This is the correct form for **G1 BPGC-E28** and probably **G2 BPGC-D22**.

---

# 2. Recommended first card

## BPGC-AIC75

A practical first board should remain inside the 75 W slot-power envelope:

| Component | Initial specification |
| --- | --- |
| Interface | PCIe 4.0 or 5.0 x16 |
| Power | Up to 75 W from slot |
| Cooling | Single-slot active or passive server heatsink |
| Processor | One BPGC-E28 or BPGC-D22 |
| Memory | 8–32 GB LPDDR or GDDR |
| SRAM | 8–32 MiB on chip |
| Primary formats | B1, ternary, INT2, INT4, W4A8, INT8 |
| Uses | Local inference, tokenization, graph analytics |
| Host support | Windows and Linux driver stack |

This card could fit ordinary workstations and many desktop systems without an auxiliary power cable.

A higher-performance version could use 150–300 W and a dual-slot cooler. PCIe CEM supports these higher power classes, though cards over 75 W require auxiliary power and have more restrictive thermal and hot-plug requirements. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fpcisig.com&size=32&drop_404_icon=true)PCI-SIG+1](https://pcisig.com/PCIExpress/Specs/CEM/CardElectromechanical_5.1_1.0?utm_source=chatgpt.com)

---

# 3. PCIe bandwidth is the host link, not the primary compute-memory link

The card should not stream every weight from system RAM for every inference operation.

Instead:

`System memory
      │
      │ PCIe transfer during model load
      ▼
Card-local GDDR / LPDDR / HBM
      │
      │ high local bandwidth
      ▼
BPU / GPC / TFME`

Model weights, tokenizer tables, embeddings, and frequently used KV-cache regions should remain on the card. PCIe carries commands, input tensors, outputs, model-loading traffic, and occasional migrations.

PCIe 6.0 supports 64 GT/s and up to 256 GB/s aggregate bidirectional bandwidth over x16. It also introduces PAM4 signaling, fixed-size flits, CRC, and lightweight forward-error correction. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fpcisig.com&size=32&drop_404_icon=true)PCI-SIG+1](https://pcisig.com/pci-express-6.0-specification?utm_source=chatgpt.com)

For the first card, PCIe 5.0 is likely the better balance:

- more mature PHY IP,
- easier board design,
- cheaper retimers and test equipment,
- broader workstation and server compatibility,
- adequate bandwidth when the card has local memory.

PCIe 6.0 should follow for the HBM and multi-chiplet generations.

---

# 4. CXL is not a different card slot

CXL uses the PCIe physical infrastructure while adding coherent processor, device-cache, and memory semantics.

That means a BPGC CXL accelerator can still physically look like a normal PCIe card:

`PCIe/CXL-compatible x16 connector
              │
        PCIe/CXL PHY
              │
       CXL Type 2 controller
              │
    ┌─────────┴──────────┐
    │                    │
BPGC compute          Local HBM`

CXL exists specifically for coherent communication among processors, accelerators, and memory-expansion devices. The current CXL 4.0 specification raises signaling to 128 GT/s, supports bundled accelerator ports, adds memory reliability features, and remains backward compatible with earlier CXL generations. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fcomputeexpresslink.org&size=32&drop_404_icon=true)Compute Express Link -+1](https://computeexpresslink.org/about-cxl/?utm_source=chatgpt.com)

---

# 5. Which CXL device type is BPGC?

## BPGC should normally be a **CXL Type 2 device**

CXL defines three principal device classes:

| CXL type | Device character | Suitability |
| --- | --- | --- |
| Type 1 | Accelerator without device-attached host-visible memory | Possible for small BPGC |
| **Type 2** | Accelerator with local memory | **Best BPGC configuration** |
| Type 3 | Memory expansion device | Best for a separate memory/PIM card |

A Type 2 device uses:

- CXL.io for enumeration, configuration, interrupts, and ordinary I/O,
- CXL.cache for coherent access to host memory,
- CXL.mem for host access to device-attached memory.

That exactly matches a BPGC accelerator containing local HBM, SRAM-PIM, tokenizer memory, and near-memory compute. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fcomputeexpresslink.org&size=32&drop_404_icon=true)Compute Express Link -+1](https://computeexpresslink.org/blog/cxl-memory-form-factor-comparison-examination-of-form-factors-for-this-growing-standard-1027/?utm_source=chatgpt.com)

---

# 6. CXL Type 2 BPGC architecture

`┌────────────────────────────────────────────────────┐
│                    HOST SYSTEM                     │
│                                                    │
│ CPU caches                                         │
│ System DRAM                                        │
│ OS / driver / CXL memory manager                   │
└───────────────────────┬────────────────────────────┘
                        │
                   CXL x16 link
                        │
┌───────────────────────▼────────────────────────────┐
│                BPGC-CXL TYPE 2 CARD                │
│                                                    │
│ CXL.io configuration and queues                    │
│ CXL.cache coherent host-memory access              │
│ CXL.mem device-memory access                       │
│ ATS / PASID / IOMMU integration                    │
│                                                    │
│ BPGC compute chiplets                              │
│ BPU / GPC / TFME                                   │
│ SRAM-PIM                                           │
│ HBM controller-side PIM                            │
│ Tokenization and embedding engines                 │
│                                                    │
│ Local HBM                                          │
└────────────────────────────────────────────────────┘`

This enables the CPU and accelerator to share data structures without constantly copying buffers through a conventional DMA-only programming model.

Examples include:

- the CPU writes an inference request into coherent memory,
- the BPGC consumes it directly,
- the host accesses BPGC-local HBM through CXL.mem,
- the accelerator caches selected host-memory structures through CXL.cache,
- graph structures and token buffers remain visible to both processors.

CXL Type 2 is also being developed for active memory tiering and near-memory compute acceleration, including migration of hot and cold pages and computation near remotely attached memory. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fcomputeexpresslink.org&size=32&drop_404_icon=true)Compute Express Link -](https://computeexpresslink.org/blog/cxl-type-2-use-cases-for-active-memory-tiering-and-near-memory-accelerators-3989/?utm_source=chatgpt.com)

---

# 7. A separate CXL-PIM memory card is also possible

The unified architecture could eventually be split across two cards:

`┌─────────────────────┐       ┌─────────────────────┐
│ BPGC Type 2 card    │       │ CXL Type 3/PIM card│
│                     │       │                     │
│ Compute chiplets    │◄─────►│ Large memory pool   │
│ Local HBM           │ CXL   │ Embeddings          │
│ BPU/GPC/TFME        │fabric │ KV cache            │
│ SRAM-PIM            │       │ Dataset buffers     │
└─────────────────────┘       │ Near-memory compute │
                              └─────────────────────┘`

A conventional Type 3 device is primarily a memory device. A BPGC-specific memory card could pair Type 3-style capacity with a management or near-memory compute engine, provided the implementation remains compliant with the selected CXL device model.

CXL memory products can use add-in-card or EDSFF form factors, and the standard supports memory expansion, pooling, sharing, and composable systems. [![Image attachment](https://t0.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https%3A%2F%2Fcomputeexpresslink.org&size=32&drop_404_icon=true)Compute Express Link -](https://computeexpresslink.org/blog/cxl-memory-form-factor-comparison-examination-of-form-factors-for-this-growing-standard-1027/?utm_source=chatgpt.com)

---

# 8. PCIe versus CXL behavior

| Feature | PCIe accelerator | CXL Type 2 accelerator |
| --- | --- | --- |
| Ordinary desktop compatibility | Excellent | Limited |
| Server compatibility | Excellent | Requires CXL-capable platform |
| Memory model | Explicit DMA | Coherent shared memory possible |
| Local card memory | Private device memory | Host-visible coherent device memory |
| Driver complexity | Moderate | Higher initially |
| Memory copying | Explicit | Can be reduced |
| Peer-memory sharing | Vendor/API dependent | Native coherent mechanisms |
| Memory pooling | No native pooling | Supported by CXL architecture |
| Best first generation | **Yes** | No |
| Best unified-memory generation | Limited | **Yes** |

The greatest CXL benefit is not raw arithmetic speed. It is eliminating unnecessary memory copies and making CPU, accelerator, and memory resources behave more like one system.

---

# 9. Consumer PCIe card versus server CXL card

## Consumer and workstation card

`BPGC-AIC
PCIe 5.0 x16
16–32 GB GDDR or LPDDR
75–150 W
one or two slots
active cooling`

Target:

- local AI inference,
- developer workstations,
- tokenization,
- retrieval,
- graph processing,
- smaller local language models,
- vision and speech.

This card should use conventional PCIe because consumer CXL platform support is not yet universal.

## Server inference card

`BPGC-HBM
PCIe 5.0/6.0 x16
32–128 GB HBM
150–300 W
passive server cooling`

Target:

- high-throughput inference,
- long-context decoding,
- recommendation systems,
- large graph workloads,
- model serving.

## Server coherent accelerator

`BPGC-CXL2
CXL Type 2
local HBM
controller-side PIM
150–300 W`

Target:

- shared CPU/accelerator memory,
- training and fine-tuning,
- large embeddings,
- unified KV-cache management,
- memory tiering,
- multi-accelerator systems.

---

# 10. Dual-mode card

The strongest long-term board could support both:

`CXL-capable host:
    negotiate CXL Type 2
    enable coherent memory
    enable CXL.cache and CXL.mem

ordinary PCIe host:
    enumerate as PCIe endpoint
    use BARs and DMA
    treat local HBM as private device memory`

This preserves compatibility while allowing advanced servers to unlock coherent operation.

The card requires:

- a PCIe/CXL-capable PHY and controller,
- conventional PCIe endpoint mode,
- CXL Type 2 mode,
- mode-dependent firmware,
- separate PCIe and CXL driver paths,
- coherent and noncoherent memory managers,
- careful reset and error-recovery design.

A pure PCIe fallback is an implementation feature rather than something to assume automatically from the connector, so it should be designed and validated deliberately.

---

# 11. Changes required in the VHDL architecture

The BPGC compute RTL does not need to change substantially. The main work occurs at the transaction and memory boundary.

## PCIe integration modules

`bpgc_pcie_endpoint_adapter.vhd
bpgc_bar_register_file.vhd
bpgc_dma_read_engine.vhd
bpgc_dma_write_engine.vhd
bpgc_msix_controller.vhd
bpgc_command_queue.vhd
bpgc_completion_queue.vhd
bpgc_pcie_error_manager.vhd`

## CXL integration modules

`bpgc_cxl_type2_adapter.vhd
bpgc_cxl_io_bridge.vhd
bpgc_cxl_cache_bridge.vhd
bpgc_cxl_mem_bridge.vhd
bpgc_coherent_region_manager.vhd
bpgc_host_device_snoop_controller.vhd
bpgc_ats_pasid_manager.vhd
bpgc_cxl_poison_ras_manager.vhd`

## Shared internal interface

Both PCIe and CXL should lower into the same internal command format:

`Host protocol
     │
PCIe or CXL adapter
     │
Unified transaction layer
     │
BPGC command processor
     │
BPU / GPC / TFME / PIM`

The internal request could contain:

`type host_transaction_t is record
    valid          : std_logic;
    transaction_id : unsigned(15 downto 0);
    operation      : host_operation_t;
    address        : unsigned(63 downto 0);
    length_bytes   : unsigned(31 downto 0);
    process_id     : unsigned(19 downto 0);
    coherent       : std_logic;
    cacheable      : std_logic;
    write_enable   : std_logic;
    completion_tag : unsigned(15 downto 0);
end record;`

The actual PCIe PHY, CXL protocol controller, high-speed SerDes, equalization, link training, and compliance blocks should come from licensed silicon IP. VHDL should model the clean transaction-side boundary above those hard blocks.

---

# 12. Board-level components

A production add-in card needs more than the BPGC ASIC:

`PCIe/CXL edge connector
PCIe/CXL retimers if required
BPGC processor package
local memory
board-management controller
SPI flash
clock generators
voltage regulators
current and temperature monitors
JTAG/debug connector
auxiliary power connector
active or passive cooling`

For an HBM version, HBM sits inside the BPGC package rather than as ordinary PCB-mounted memory:

`PCB
 └── BPGC package
      ├── compute die
      ├── memory-controller die
      ├── HBM stacks
      └── silicon interposer or advanced substrate`

The PCIe or CXL card then becomes the carrier for the complete advanced package.

---

# 13. Recommended roadmap

## Generation A: PCIe FPGA card

- existing VHDL,
- FPGA PCIe hard IP,
- DDR or HBM development board,
- Linux driver,
- command queues,
- DMA,
- PyTorch/ONNX runtime.

## Generation B: PCIe 5.0 ASIC card

- 28 nm or 16/12 nm BPGC,
- 75–150 W,
- LPDDR or GDDR,
- standard CEM board,
- PCIe-only software stack.

## Generation C: PCIe HBM card

- chiplet-based processor,
- local HBM,
- 150–300 W,
- PCIe 5.0 or 6.0,
- training-lite and large-model inference.

## Generation D: CXL Type 2 card

- coherent host-memory access,
- host-visible local HBM,
- accelerator memory tiering,
- controller-side PIM,
- server/workstation focus.

## Generation E: CXL fabric system

- several BPGC Type 2 cards,
- CXL switches,
- CXL Type 3 capacity memory,
- pooled embeddings and KV cache,
- unified training/inference cluster.

---

# Verdict

$$
\boxed{
\text{Yes, PCIe AIC should be the first physical product.}
}
$$

It is the cheapest, most compatible, and fastest route from VHDL to usable hardware.

$$
\boxed{
\text{CXL Type 2 should be the later unified-memory product.}
}
$$

CXL should not replace the initial PCIe card. It should evolve it.

The ideal end-state is a card that looks physically like a GPU, behaves as a normal PCIe accelerator in ordinary systems, and unlocks coherent CPU-memory-accelerator operation when installed in a CXL-capable server. One board, two personalities, and a great deal of binary thunder behind the bracket. ⚡

A quarterly PCIe 6.0, CXL 4.0, host-platform, controller-IP, and compliance update would keep the board roadmap aligned with the rapidly changing ecosystem.

---

## Message 17 — User

<!-- Message ID: bbb21e55-eaff-4f69-8487-6101d56b938c; Turn index: 8 -->

We're still building the drivers and kernel but the BCIR could eventually replace Linux as an orchestrator and be the software backend that provides the coherent shared memory https://github.com/Mecca-Research/Binary-Correspondence-Intermediate-Representation  GitHub

---

## Message 18 — Assistant

<!-- Message ID: becf3a17-cbfe-4aee-843f-ccbb310df614; Turn index: 8 -->

Yes. **BCIR can evolve from an IR and verified runtime into the orchestration authority for the entire BPGC compute-memory fabric.** The repository is already moving in that direction.

The important refinement is this:

> BCIR should not initially replace Linux as a general-purpose operating system. It should first replace Linux’s role as the **accelerator orchestrator, memory-placement authority, and device-programming control plane**.

Linux can remain underneath as a hardware compatibility scaffold until the native BCIR kernel is mature. Eventually, Linux could become an optional compatibility guest, recovery environment, or development host rather than the system’s brain.

# The architecture already has the correct foundation

The repository currently separates BCIR into:

- a Python conformance oracle,
- an MLIR law and verifier rail,
- a freestanding C runtime,
- K_BCIR placement and cost optimization,
- GEM scheduling,
- immutable StreamPack execution artifacts,
- device manifests, memory banks, channels, and generations. 

The master roadmap explicitly defines BCIR as platform-neutral. Linux, Windows, POSIX, vendor runtimes, hosted allocation, and IPC are intended to be adapters around BCIR’s direct contracts rather than the definition of the system. 

Even more importantly, the driver roadmap already identifies three distinct products:

1. The BCIR Driver Development Kit.
2. BCIR-Linux as a compatibility and measurement environment.
3. A native BCIR kernel with capability-scoped services, slim IPC, and native driver adapters. 

So your new chip architecture does not require us to twist BCIR into something alien. It actually gives BCIR the physical machine it has been quietly preparing to govern.

# BCIR should become the **Fabric Operating System**

A useful name for the target layer would be:

## BCIR Fabric Kernel

Its job would not primarily be windows, files, shells, and desktop processes. Its primary objects would be:

- tensors,
- graphs,
- token streams,
- model layers,
- command graphs,
- memory regions,
- StreamPacks,
- BPGC tiles,
- HBM channels,
- PIM engines,
- CXL regions,
- PCIe endpoints,
- optical links,
- accelerator contexts.

The whole machine would look like this:

```text
Applications / AI frameworks / model servers
                    │
                    ▼
             BCIR Frontends
      ONNX, MLIR, C, Python, model ingest
                    │
                    ▼
        BCIR Claims and Registry
                    │
                    ▼
       K_BCIR Planning and Admission
 legality → placement → schedule → budget
                    │
                    ▼
       GEM and StreamPack Generation
                    │
                    ▼
          BCIR Fabric Kernel
 memory ownership, devices, DMA, CXL,
 synchronization, telemetry, recovery
                    │
        ┌───────────┼────────────┐
        ▼           ▼            ▼
      BPGC        HBM/PIM      CPU/GPU
      cards       memory       fallback
```

Linux initially occupies the lower box. As BCIR drivers and kernel services mature, the BCIR Fabric Kernel progressively takes it over.

# Semantic coherence versus physical cache coherence

This distinction is crucial.

BCIR cannot create electrical cache coherence merely by declaring memory coherent. The actual mechanism must come from:

- a CXL controller,
- CPU cache controllers,
- device caches,
- IOMMU and address translation,
- DMA fencing,
- PCIe peer-memory support,
- HBM controllers,
- hardware invalidation and synchronization.

What BCIR can provide is a stronger **semantic coherence model** above those mechanisms.

In PCIe mode, BCIR would enforce coherence through:

- explicit ownership,
- DMA completion,
- fences,
- generation changes,
- cache flushes,
- invalidation,
- copy or migration records.

In CXL mode, the same BCIR model could lower to hardware-supported shared memory and coherent mappings.

This gives us one software model with multiple physical realizations:

```text
BCIR semantic operation:
    acquire object for device write

PCIe realization:
    flush host cache
    pin pages
    DMA object
    transfer ownership
    signal device

CXL realization:
    establish shared mapping
    assign device context
    issue coherent acquire
    execute through shared region
```

That is precisely BCIR’s correspondence principle applied to memory.

# The HAM subsystem is already the embryonic memory fabric

The repository’s Hierarchical Access Memory subsystem already models:

- content-addressed semantic resources,
- dependencies,
- home banks,
- sizes,
- priorities,
- mutability,
- generations,
- directed hardware links,
- direct-peer, direct-DMA, staged, and host-bounce routes,
- residency and eviction,
- write invalidation,
- write-back,
- capacity replay,
- route-cost verification. 

Mutable objects advance their generation when written, invalidate stale copies, and return the current generation to their home bank. Dependencies must be resident and current before an operation may execute. 

That is already much closer to a coherent object system than an ordinary compiler memory model.

What is missing is the physical adapter layer. The repository is honest that no actual CXL, PCI peer-memory, GDS, NVMe, or controller binding currently exists. 

# BCIR Coherent Memory Objects

The next major software abstraction should be a **BCIR Coherent Memory Object**, or CMO.

```text
BCIRMemoryObject {
    object_id
    object_type
    virtual_address
    byte_length
    shape
    layout
    precision

    home_bank
    resident_banks
    owner
    permitted_readers
    coherence_scope

    generation
    data_hash
    lineage_hash
    dirty_state
    active_fences

    placement_constraints
    permitted_engines
    security_domain
    fault_state
    provenance
}
```

Possible ownership states:

```text
UNMAPPED
HOST_EXCLUSIVE
DEVICE_EXCLUSIVE
SHARED_READ_ONLY
SHARED_COHERENT
PIM_EXECUTING
MIGRATING
WRITEBACK_PENDING
POISONED
RECOVERY
```

Core operations:

```text
cmo.create
cmo.map
cmo.acquire_read
cmo.acquire_write
cmo.publish
cmo.invalidate
cmo.migrate
cmo.prefetch
cmo.fence
cmo.snapshot
cmo.rollback
cmo.release
```

The same CMO could represent:

- a model-weight tensor in HBM,
- an activation in tile SRAM,
- a KV-cache page in CXL memory,
- a graph adjacency block,
- a tokenizer vocabulary table,
- an optimizer state,
- a StreamPack,
- a remote object on another accelerator.

# BCIR becomes the true scheduler

Linux schedulers primarily schedule CPU threads. BCIR would schedule **state transformations across a heterogeneous physical graph**.

The orchestration decision would be:

\[
\Pi^*
=
\arg\min_{\Pi \in Legal(G,H)}
\left[
T_\Pi+
\lambda E_\Pi+
\mu D_\Pi+
\nu R_\Pi
\right]
\]

subject to:

- memory capacity,
- thermal limits,
- power limits,
- bandwidth,
- coherence legality,
- security domains,
- device availability,
- numerical precision,
- latency deadlines.

The scheduler chooses:

- which BPGC tile,
- which tensor engine,
- whether to use SRAM-PIM or HBM-PIM,
- where the object should reside,
- whether migration is worthwhile,
- whether the CPU or GPU fallback is preferable,
- when a CXL region should be acquired,
- whether a graph phase may overlap a tensor phase,
- how a failed device is bypassed.

This is more than a kernel task scheduler. It is a **verified physical-realization scheduler**.

# How Linux gradually disappears

The repository’s own dependency order provides the correct escape path:

1. Stable compiler and runtime contracts.
2. Direct `RuntimeChannel`.
3. Real simple device driver.
4. Linux-hosted adapter.
5. Virtio queue and DMA proofs.
6. Stable UAPI.
7. BCIR-Linux experiments.
8. Native adapter and slim IPC. 

The canonical driver sequence starts with telemetry, UART, Linux parity, virtio-console, virtio-blk, and only freezes the UAPI after both MMIO/event and queue/DMA lifecycles have been exercised. 

That gives us a natural five-stage evolution.

## Stage 1: Linux-hosted BCIR runtime

Linux owns:

- boot,
- PCIe enumeration,
- memory allocation,
- interrupts,
- IOMMU,
- CXL management,
- process isolation.

BCIR owns:

- model compilation,
- command generation,
- scheduling,
- StreamPacks,
- accelerator placement,
- semantic memory generations.

## Stage 2: BCIR-Linux orchestration layer

BCIR gains:

- kernel module,
- device node and stable UAPI,
- DMA and event queues,
- memory-object registry,
- CXL topology adapter,
- P2P and dma-buf adapters,
- device telemetry,
- process/context isolation.

Linux is still the kernel, but BCIR becomes the AI subsystem’s operating authority.

## Stage 3: BCIR fabric supervisor

A privileged BCIR service owns:

- all BPGC cards,
- HBM and PIM resources,
- accelerator memory placement,
- CXL memory tiers,
- command admission,
- model lifecycle,
- tokenization pipelines,
- checkpoint and recovery state.

Linux becomes a host for the supervisor rather than the decision-maker.

## Stage 4: Native BCIR accelerator kernel

The native kernel gains:

- boot and initialization,
- page tables and MMU management,
- interrupts,
- timers,
- capability handles,
- IOMMU control,
- direct PCIe/CXL drivers,
- slim IPC,
- memory ownership,
- hardware fault recovery.

This is initially suited to dedicated BPGC appliances and servers, where hardware configurations are controlled.

## Stage 5: Linux as optional compatibility guest

The final structure could be:

```text
BCIR Fabric Kernel
├── Native BCIR model services
├── Native BPGC drivers
├── Native CXL/HBM manager
├── Native inference and training runtime
├── Linux compatibility VM
└── Recovery and diagnostics environment
```

BCIR would own the machine. Linux would provide compatibility for software that still expects POSIX, filesystems, networking tools, package managers, and existing drivers.

# The kernel should remain microkernel-like

BCIR should avoid replacing Linux with another giant monolith.

The native kernel should contain only the mechanisms that require privilege:

- address spaces,
- capabilities,
- interrupts,
- IOMMU,
- memory mappings,
- DMA,
- device lifecycle,
- secure IPC,
- timers,
- fault containment.

Higher-level services should remain separate:

```text
BCIR native kernel
    │
    ├── Memory Fabric Service
    ├── Device Registry Service
    ├── BPGC Driver Service
    ├── CXL Region Service
    ├── StreamPack Executor
    ├── Telemetry Service
    ├── Model Lifecycle Service
    └── Tokenization Service
```

A crashed tokenizer should not take down the memory manager. A failed model server should not corrupt the CXL topology registry. A defective device driver should be contained by capabilities and IOMMU mappings.

# What must be added to BCIR

The largest remaining components are not compiler opcodes. They are system-lifetime machinery.

## Kernel fundamentals

- physical and virtual memory managers,
- page tables,
- interrupt and exception handling,
- timer subsystem,
- kernel scheduler,
- capability system,
- IPC,
- process/service lifecycle,
- SMP startup,
- synchronization primitives,
- power and reset management.

The current repository explicitly labels the native boot, memory, interrupt, process/service, transport, and IPC implementations as missing. 

## Device lifecycle

- enumeration,
- probing,
- hotplug,
- reset,
- suspend and resume,
- firmware loading,
- device disappearance,
- stale handle invalidation,
- peer failure,
- cancellation,
- poison and error recovery.

## Memory fabric adapters

The HAM roadmap already gives the correct sequence:

- capability/topology adapter,
- optional direct-storage adapter,
- PCI peer-memory and dma-buf bridge,
- CXL region and tier adapter,
- NVMe semantic store,
- controller co-design only after host-managed evidence exists. 

## Security

- signed StreamPacks,
- capability-scoped device access,
- PASID/process separation,
- IOMMU enforcement,
- firmware attestation,
- model and weight provenance,
- tenant isolation,
- encrypted memory where available,
- replay-resistant command queues,
- secure recovery.

# Recommended repository additions

A clean next architecture slice could add:

```text
docs/kernel/
    BCIR_COHERENT_MEMORY_MODEL.md
    BCIR_FABRIC_KERNEL_ARCHITECTURE.md
    BCIR_CXL_TYPE2_ADAPTER.md
    BCIR_DEVICE_CAPABILITY_MODEL.md

bcir/kbcir/
    coherent_memory.py
    fabric_scheduler.py
    cxl_topology.py
    ownership.py

runtime/c/
    bcir_cmo.h
    bcir_cmo.c
    bcir_fabric_channel.h
    bcir_fabric_channel.c
    bcir_ownership.h
    bcir_ownership.c

drivers/linux/
    bcir_core/
    bcir_pcie/
    bcir_cxl/
    bcir_bpgc/

kernel/native/
    boot/
    mm/
    irq/
    capability/
    ipc/
    iommu/
    pci/
    cxl/
    services/
```

The first real implementation target should be modest:

> One Linux-hosted BPGC PCIe device, one BCIR memory-object registry, one DMA queue, one command queue, one completion queue, and deterministic ownership transitions.

Then add a CXL-backed shared object without changing the BCIR-level program. That would be the decisive proof that BCIR correspondence works across both explicit DMA and coherent memory.

# Final judgment

The idea is not merely plausible. It is the coherent completion of the current architecture.

BCIR already has:

- semantic claims,
- legal realization selection,
- device and bank registries,
- generation-tagged artifacts,
- scheduling,
- provenance,
- explicit data movement,
- replay verification,
- a freestanding runtime,
- a native-kernel roadmap.

Its HAM layer already models the semantic half of unified memory, while honestly leaving the hardware half to future drivers and kernel adapters. 

The final system should therefore be framed as:

\[
\boxed{
\text{BCIR}
=
\text{compiler}
+
\text{orchestrator}
+
\text{memory-fabric authority}
+
\text{verified execution kernel}
}
\]

Linux is the launch gantry. BCIR is the spacecraft. We should use the gantry for as long as it helps us climb, then leave it standing below when the native system can finally hold orbit on its own. 🚀

---
