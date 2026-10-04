# Circuit design breakdown

Source: https://chatgpt.com/g/g-685d6dcc1298819182932c85d5f3420e-gpt-evolve/c/687694ab-29f8-8011-bba2-6d0c75727733?src=history_search

Recovered: October 4, 2026.

Messages: 4 (2 user; 2 assistant).

Recovery verification: opened the source separately, repeatedly loaded older messages to the opening, and verified the final source message. Overlapping captured batches form one continuous chronological sequence. Message IDs were deduplicated; original wording and errors are retained with formatting reconstructed as Markdown. Attachment binaries and alternate branches are not embedded.

---

## 1. User

<!-- Message ID: user-before-a835ddec-707d-4451-a6b8-ea6df295a446 -->

Give a full breakdown of the circuit design provided in the image

---

## 2. Assistant

<!-- Message ID: a835ddec-707d-4451-a6b8-ea6df295a446 -->

Below is a “walk-the-net” explanation of every device and signal you can see in the screenshot.
(For clarity I’ve labelled the key internal nodes; the colours correspond to the schematic.)



---



### 1.  High-level role

The block is a **bootstrapped sample-and-hold (S/H) switch** intended for an analog front-end (e.g., an ADC input track-and-hold).  



- During **track** the switch behaves like a very-low-R<sub>on</sub> transmission gate, copying vin onto the sampling capacitor at vout.

- During **hold** both devices turn off and the capacitor stores the last sampled value.



---



### 2.  External pins



| Pin | Function |
| --- | --- |
| vin | Analog input you want to sample |
| vout | Stored / tracked output (right-hand side, tied to the sampling capacitor) |
| clk | Non-overlapping sampling clock (goes high during “track”) |
| vdd, vss | Supply rails |



---



### 3.  Core transmission gate



| Device | Type | Gate drive | Purpose |
| --- | --- | --- | --- |
| **M<sub>P</sub>** (upper) | PMOS | clk̅ | Passes the *high* portion of the signal |
| **M<sub>N</sub>** (lower) | NMOS | clk (boosted) | Passes the *low* portion of the signal |

Together they form the bidirectional switch between the vertical vin bus (left) and the horizontal vout bus (centre-right).



---



### 4.  Bootstrapping network (boxed loop at the top)



| Block | What it does | Why it matters |
| --- | --- | --- |
| **Inverter** | Generates the true / complement clock pair (clk and clk̅). |  |
| **Flying capacitor C<sub>B</sub>** (between the little “plate” symbol and the wire returning to the gates) | During the **hold** phase it pre-charges to **V<sub>DD</sub>**. When **clk** rises, the bottom plate is *kicked* up by almost V<sub>DD</sub>, so the top plate (and thus the NMOS gate) is driven to ≈ vin + V<sub>DD</sub>. |  |
| **Switch devices S<sub>1</sub>, S<sub>2</sub>** inside the loop | Connect/disconnect the capacitor to the proper nodes so the boost only occurs during “track”, and the cap re-charges during “hold”. |  |

**Result:** the NMOS gate sees **V<sub>GS</sub> ≃ V<sub>DD</sub>** over the entire input swing, so the switch R<sub>on</sub> stays low even when vin ≈ V<sub>DD</sub> – V<sub>TH</sub>. That keeps distortion and droop small.



---



### 5.  Sampling capacitor

The small two-plate symbol tied between vout and vss is **C<sub>S</sub>**, the hold capacitor.
*Its size sets the kT/C noise floor and the aperture error of the sample-and-hold.*



---



### 6.  How one clock cycle works



| Phase | clk | Actions in the switch |
| --- | --- | --- |
| **Track (clk = 1)** | 1 | *S<sub>1</sub>* opens, *S<sub>2</sub>* closes → C<sub>B</sub> is now in series with M<sub>N</sub> gate, boosting it. M<sub>P</sub> is on because clock-bar is low. vin is transferred to vout. |
| **Hold (clk = 0)** | 0 | M<sub>P</sub> off, M<sub>N</sub> gate pulled back to ground (boost disabled). *S<sub>1</sub>* closes, *S<sub>2</sub>* opens so C<sub>B</sub> re-charges to V<sub>DD</sub>. The transmission gate is open-circuited and C<sub>S</sub> remembers the sample. |



---



### 7.  Design considerations & tricks you can spot



1. **Charge injection balance** – the complementary pair (PMOS + bootstrapped NMOS) cancels most feed-through when the devices turn off.

2. **Clock feed-through reduction** – the little loop routing the boosted node back to the PMOS gate keeps the gates at similar dV/dt, reducing kick-back on vin.

3. **Layout symmetry** (implied) – in silicon this cell is usually laid out so the vin line runs straight through, with the PMOS directly over the NMOS to minimise parasitic mismatches.

4. **Sky-130 PDK symbols** – the colour convention and transistor symbols suggest this schematic targets the open-source SkyWater 130 nm process (note the file tab “sky130_ef_ip_sah.sch” at top left).



---



### 8.  Performance quick notes (rule-of-thumb)



| Metric | What dominates | How to tweak |
| --- | --- | --- |
| **On-resistance** R<sub>on</sub> | M<sub>N</sub> width, boost voltage margin | Increase W/L, ensure C<sub>B</sub> ≳ 100 fF for full boost |
| **Sample droop** | C<sub>S</sub> leakage, M<sub>P/N</sub> off-leakage | Use larger C<sub>S</sub>; guard-ring around the hold node |
| **kT/C noise** | C<sub>S</sub> | Double C<sub>S</sub> → −3 dB noise |
| **Linearity (THD)** | R<sub>on</sub>(vin) flatness | Proper bootstrapping & matched devices |



---



### 9.  When you might ***not*** need the bootstrap

If your input never gets within ~0.5 V of V<sub>DD</sub> or your speed / linearity targets are relaxed, a simple CMOS transmission gate (no booster) is cheaper. But for ≥ 10-bit ADCs or rail-to-rail sensor inputs, this bootstrapped topology is the standard choice.



---

**Bottom line:**
This schematic is a textbook *bootstrapped CMOS transmission gate* used as the sampling switch of a precision sample-and-hold. The extra capacitor and switch pair form a tiny charge pump that “lifts” the NMOS gate during the track phase, keeping the on-resistance low and flat across the entire input range, while the complementary PMOS ensures rail-to-rail conduction and good charge-injection cancellation.

---

## 3. User

<!-- Message ID: user-before-c0cd3af4-af66-4583-8252-0404e876d657 -->

All right, we need to take it back several steps. It seems like this diagram is too far ahead and advanced. Let's break down textbook CMOS transmission gate design. What are the basic fundamental building blocks components? What are the goals that are attempted to achieve in the design process? And what are known popular strategies and approaches on getting the features you want out of your circuit design?

---

## 4. Assistant

<!-- Message ID: c0cd3af4-af66-4583-8252-0404e876d657 -->

Below is a “from-scratch” view of a **CMOS transmission gate (TG)** that you would find in first-year VLSI or mixed-signal design texts.  I’ve split the answer into (1) the essential pieces, (2) the design objectives, and (3) the well-worn tricks engineers use to hit those objectives.



---



## 1  | Fundamental building blocks



| Block | Symbolic view | Why it’s there |
| --- | --- | --- |
| **NMOS transistor** |  | Passes strong ‘0’, weak ‘1’.  Low R<sub>on</sub> when input is near 0 V. |
| **PMOS transistor** |  | Passes strong ‘1’, weak ‘0’.  Low R<sub>on</sub> when input is near V<sub>DD</sub>. |
| **Complementary gate drives** | φ and φ̅ (non-overlapping clock or logic level) | Turn the two devices **on** and **off** together while preventing shoot-through. |
| **Body (substrate) ties** | N-well tie for PMOS, P-substrate tie for NMOS | Protect against body-effect threshold shift and latch-up. |
| *(Optional)* **Dummy / cancellation devices** | Half-sized FETs whose gates switch oppositely | Cancel charge injection when the gate turns off. |
| *(Optional)* **Bootstrapping cap + helper FETs** | Charge-pump loop | Keeps NMOS gate ≈ input + V<sub>DD</sub> for rail-to-rail input (needed in precision analog). |

Schematic “core” (textbook version, no bells & whistles):

`             φ̅
 VDD ───┬───|◀|───┐
        │   M_P   │
 in ────┼──────────┼─── out
        │   M_N   │
 VSS ───┴───|▶|───┘
             φ`*Both FETs are in parallel; gates are driven by opposite-phase control.*



---



## 2  | Design goals (what you’re trying to achieve)



| Goal | What it means in practice | Typical spec knob |
| --- | --- | --- |
| **Rail-to-rail conduction** | Pass a logic ‘1’ that reaches ≥ V<sub>DD</sub>–0.1 V and a ‘0’ ≤ 0.1 V. | Use complementary pair; add bootstrap if the swing must be *exactly* rail-to-rail. |
| **Low on-resistance (R<sub>on</sub>)** | Keep R<sub>on</sub> × C<sub>load</sub> below τ<sub>max</sub> for timing/bandwidth. | Increase W/L, stack devices, or use boost. |
| **High off-isolation / low leakage** | When disabled, leakage ≪ desired hold time current. | Reduce W/L, use series devices, bias body, add shielding transistors. |
| **Low charge injection & feed-through** | When you switch, ΔV on the line should be < LSB (for ADC) or below logic noise margin. | Use matched devices, dummy FETs, symmetric layout, slower edge on φ̅. |
| **Symmetry / bidirectionality** | Able to pass signals either way with the same performance. | Mirror-image layout, ensure equal parasitics. |
| **Minimal area & power** | Especially in large MUX trees. | Size to what timing really needs; share well contacts; power-gate drivers. |



---



## 3  | Popular strategies & design approaches



### 3.1  Sizing & first-order analysis



1. **Write the RC budget**:
  $R_{on,max} = \frac{τ_{max}}{C_{load}}$
  where τ<sub>max</sub> is the allowable 1/e settling time or ﻿$\frac{ln(2)}{f_{-3dB}}$﻿ for analog.

2. **Compute device width** with the worst-case V<sub>GS</sub>:
  $R_{on} ≈ \frac{1}{μC_{ox}(W/L)(V_{GS}-V_T)}$
  Take the *smaller* of V<sub>GS</sub> cases (NMOS at high input, PMOS at low input).

3. **Iterate in SPICE**, sweeping PVT corners.  Increase W until every corner meets spec, then add 10–15 % guard-band for layout parasitics.



### 3.2  Reducing charge injection



- **Complementary pair itself** cancels about half the gate-charge kick (signs oppose).

- **Dummy (series) FETs**: put a half-sized device in series whose gate sees the *opposite* phase; its injection cancels the first device.

- **Staggered edge**: delay φ̅ a few hundred picoseconds so both gates don’t fall simultaneously.



### 3.3  Keeping R<sub>on</sub> flat across the swing



- **Bootstrapping** (a tiny charge-pump cap) – classic for track-and-hold front-ends, as described in the previous answer.

- **Parallel fingered layout** – identical segments wired in parallel; helps with litho variation and reduces mismatch.



### 3.4  Body-effect & well strategy



- **PMOS in its own N-well** lets you tie body to V<sub>DD</sub>, keeping |V<sub>TP</sub>| constant.

- **Deep-N-well guard** around sensitive nodes reduces substrate noise injection (mixed-signal chips).



### 3.5  Clock generation



- **Non-overlap windows** ensure one device turns completely off before the other turns on, preventing short VDD-to-GND paths.

- **Level shifters** if φ exceeds core supply (e.g., 1.2 V logic driving a 3.3 V TG).



### 3.6  Layout “must-dos”



1. **Match device lengths** and orient diffusion in the same direction → charge injection cancels.

2. **Symmetric routing** of in and out under the gate fingers → equal RC on both sides.

3. **Common centroid or interdigitated fingers** when you have multiple TGs in an array (MUX, DAC).

4. **Guard rings & tap every few fingers** to kill latch-up and substrate noise.



---



## 4  | Putting it together – a beginner’s flow



1. **Write a requirements table** (voltage range, speed/bandwidth, leakage, area).

2. **Hand-back-of-the-envelope**: pick C<sub>load</sub>, get R<sub>on</sub>, choose W/L.

3. **First SPICE deck**: ideal clocks, typical corner, observe transfer curve & R<sub>on</sub>(Vin).

4. **Add realistic edges & parasitics**: non-overlap, driver resistance, wiring C’s.

5. **Crank corners** (SS, FF, TT, temperature extremes).

6. **Add cancellation tweaks** only if metrics fail (avoid over-complicating early).

7. **Finalize layout** with matched fingers, run extracted-netlist sims.

8. **Sign-off**: Monte-Carlo mismatch, ESD checks, gate-oxide reliability (V<sub>GS,max</sub>), latch-up.



---



### Cheat-sheet of typical choices



| Application | Supply | Swing needed | Extras you’ll likely add |
| --- | --- | --- | --- |
| Digital MUX tree | 1.0 V core | Logic-level only | None – plain TG, minimum W/L to meet timing. |
| SAR-ADC sampling switch (10–12 bit) | 1.8 V | Rail-to-rail sensor input | Bootstrapped NMOS + dummy FETs. |
| Video bandwidth (~100 MHz) sample-hold | 3.3 V | 0–1 V swing | Wider devices, no bootstrap, but strong non-overlap control to cut feed-through. |
| Low-power IoT (kHz) | 0.9 V | 0–0.9 V | Small W/L, leakage-driven design, series TGs for off-isolation. |



---

**Key takeaway:**
A CMOS transmission gate is nothing more than an NMOS and PMOS in parallel with opposite gate drives.  From that simple nucleus you add **sizing**, **clock phasing**, and (if you need precision) **injection-cancellation or bootstrapping** layers.  Each extra feature directly targets one of the core goals: lower R<sub>on</sub>, better swing, smaller charge kick, or lower leakage.  Starting simple, validating specs, and adding complexity only where the numbers force you to is the textbook—and still the professional—way to design a robust TG.
