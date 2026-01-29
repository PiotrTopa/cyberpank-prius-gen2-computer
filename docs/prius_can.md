# Prius Gen2 (2009) CAN/OBD2 PID Codes

## 1. Overview and Hardware Context
[cite_start]This compilation covers CAN/OBD2 PID codes verified on a Gen2 2009 Prius[cite: 1, 2].
* [cite_start]**Credits:** Compilation includes work from Atilla Vass and usbseawolf2000[cite: 3, 4].
* [cite_start]**Hardware Reference:** Arduino Mega2560 with a CAN-bus shield connected to the OBD2 connector[cite: 5].
* [cite_start]**Protocol:** ISO 15765-2 CAN multi-frame messages[cite: 6].
* **Message Types:**
    * **OBD2 (Un-solicited):** Broadcast by ECUs during normal activity (~1500/sec). [cite_start]ID contains Mode and PID[cite: 8, 9].
    * **CAN (Solicited):** Generic or specific requests sent to an ECU address (e.g., 0x07E0). [cite_start]Mode and PID are embedded in data bytes[cite: 8, 10].

---

## 2. Message Structure Conventions
[cite_start]**Conventions:** #: decimal, 0x# hexadecimal (one # per 4-bit nibble)[cite: 13].

**Library Message Structure:**
* [cite_start]**ID:** Unsigned Int (2-byte)[cite: 15].
* [cite_start]**Header:** Byte[cite: 18].
* [cite_start]**Data:** 8 Bytes (A through H) [cite: 19-28].
* [cite_start]**Data Length:** Bits 3..0[cite: 29].

**OBD2 Message Format:**
* [cite_start]**ID:** Mode + PID (e.g., Car Speed 0x03CA)[cite: 32, 46].
* [cite_start]**Data:** A..H (e.g., 00x5 indicates length) [cite: 35-48].

**CAN PID Request Message (Solicited):**
* [cite_start]**ID:** ECU Address (e.g., 0x07E0 Engine, 0x07E2 Hybrid)[cite: 57, 71].
* **Data Payload:**
    * [cite_start]Length (Bits 3..0)[cite: 69, 70].
    * [cite_start]Mode (e.g., 0x01 or 0x21)[cite: 60, 74].
    * [cite_start]PID (e.g., 0x00, 0xC3)[cite: 61, 84].

**Multi-Frame Logic:**
1.  **Single Frame (SF):** Fits in one message. [cite_start]Info byte 0x0#[cite: 90].
2.  **First Frame (FF):** Start of multi-frame. [cite_start]Info byte 0x1# + Length[cite: 115].
3.  **Flow Control (FC):** Request for further frames. [cite_start]Info byte 0x30[cite: 134].
4.  **Consecutive Frames (CF):** Subsequent data. [cite_start]Info byte 0x2#[cite: 158].

---

## 3. ECU Addresses
* [cite_start]**0x727:** Transmission [cite: 201, 202]
* [cite_start]**0x750:** Main Body [cite: 203, 204]
* [cite_start]**0x780:** AirBag [cite: 205, 206]
* [cite_start]**0x781:** Precrash [cite: 207, 208]
* [cite_start]**0x790:** Distance [cite: 209, 210]
* [cite_start]**0x791:** Precrash2 [cite: 211, 212]
* [cite_start]**0x7A1:** Steering Assist [cite: 213, 214]
* [cite_start]**0x7A2:** Park Assist [cite: 215, 216]
* [cite_start]**0x7B0:** ABS Brake [cite: 217, 218]
* [cite_start]**0x7C0:** Instrument [cite: 219, 220]
* [cite_start]**0x7C4:** Air Conditioner [cite: 221, 222]
* [cite_start]**0x7D0:** Navigation [cite: 223, 224]
* [cite_start]**0x7E0:** Engine Controls [cite: 225, 226]
* [cite_start]**0x7E2:** Hybrid System [cite: 227, 228]

---

## 4. Un-Solicited (OBD2) Prius Specific Codes
*Note: Un-Cal = Uncalibrated.*

* [cite_start]**Lateral acceleration** (PID: 0022) [cite: 229]
    * Eq: `(256*A+B) - 0x0200` | Unit: Un'Cal | 13ms
* [cite_start]**Longitudinal acceleration** (PID: 0023) [cite: 229]
    * Eq: `(256*A+B) - 0x0200` | Unit: Un'Cal | 13ms
* [cite_start]**Steering angle** (PID: 0025) [cite: 229]
    * Eq: `(256*A+B)` | Unit: Un'Cal (12bit signed, straight-ahead is vehicle specific) | 13ms
* [cite_start]**Brake Pedal Position** (PID: 0030) [cite: 229]
    * Eq: `E` | Range: 0-127 | 6ms | *Note: May be 0x7F when not pressed or on startup.*
* [cite_start]**ICE Temperature** (PID: 0039) [cite: 229]
    * Eq: `A` | Range: 0-255 °C | 88ms | *Note: Use solicited 07E00105 instead.*
* [cite_start]**EM Amp** (PID: 003B) [cite: 229]
    * Eq: `(256*A+B)/10` | 12bit signed integer.
* [cite_start]**EM Volt** (PID: 003B) [cite: 229]
    * Eq: `D` | Range: 0-255V (Nominal 201V) | 8ms
* [cite_start]**Front Right Wheel Pulses** (PID: 0081) [cite: 229]
    * Eq: `256*A+B` | Range: 0-65535 | 13ms | *Note: 185 pulses/rev.*
* [cite_start]**Front Left Wheel Pulses** (PID: 0081) [cite: 229]
    * Eq: `256*C+D` | Range: 0-65535 | 13ms
* [cite_start]**Rear Right Wheel Pulses** (PID: 0083) [cite: 229]
    * Eq: `256*A+B` | Range: 0-65535 | 13ms
* [cite_start]**Rear Left Wheel Pulses** (PID: 0083) [cite: 229]
    * Eq: `256*C+D` | Range: 0-65535 | 13ms
* [cite_start]**Drive Mode** (PID: 0120) [cite: 229]
    * Eq: `(D:0-1)` | Range: 0-3 | 17ms | *Binary: P=0x0, R=0x1, N=0x2, D=0x3.*
* [cite_start]**Throttle pedal position** (PID: 0244) [cite: 229]
    * Eq: `G` | Range: 0-0xC8 | 25ms
* [cite_start]**ICE Speed (Target)** (PID: 03C8) [cite: 229]
    * Eq: `256*C+D` | Range: 0-65535 RPM | 70ms
* [cite_start]**Vehicle Speed** (PID: 03CA) [cite: 229]
    * Eq: `C` | Range: 0-255 km/hr | 108ms
* [cite_start]**HV SOC %** (PID: 03CB) [cite: 229]
    * Eq: `(256*C+D)/2` | Range: 0-100% | 108ms | *Note: MFD displays 40-80% range.*
* [cite_start]**HV Max Discharge Current** (PID: 03CB) [cite: 229]
    * Eq: `A` | Range: 0-255 Amp | 108ms | *Nominally 100A <51°C.*
* [cite_start]**HV Max Charge Current** (PID: 03CB) [cite: 229]
    * Eq: `B` | Range: 0-255 Amp | 108ms | *Nominally 105A <51°C.*
* [cite_start]**Ignition Timing Advance** (PID: 0526) [cite: 229]
    * Eq: `B.C?` | 530ms | *Note: Use 07E0010E.*
* [cite_start]**0x00000529 Msg Event** (PID: 0529) [cite: 229]
    * Eq: `(A:7)` | 1000ms | *Immediate msg on event.*
* [cite_start]**General Problem (Triangle)** (PID: 0529) [cite: 229]
    * Eq: `(B:2,4,6)` | Binary 1 beep, red triangle.
* [cite_start]**Not in Park / Driver Door Open** (PID: 0529) [cite: 229]
    * Eq: `(B:3)`
* [cite_start]**MFD SOC Bars** (PID: 0529) [cite: 229]
    * Eq: `(D:0-2)` | 0-8 bars.
* [cite_start]**EV-Mode Active** (PID: 0529) [cite: 229]
    * Eq: `(E:6)`
* [cite_start]**EV-Mode Denied** (PID: 0529) [cite: 229]
    * Eq: `(F:5,6,7)` | MFD: "Cannot change to EV mode now".
* [cite_start]**ICE Temperature** (PID: 052C) [cite: 229]
    * Eq: `B/2` | Range: 0-127 °C | 1050ms
* [cite_start]**Headlights Status** (PID: 057F) [cite: 229]
    * Eq: `(B:3-5)` | 1050ms | *Off: 0x00, Parkers: 0x10, Low: 0x30, High: 0x38.*
* [cite_start]**Fuel Tank Level** (PID: 05A4) [cite: 229]
    * Eq: `B` | Range: 0-0x20 (~44L) | 3400ms
* [cite_start]**Doors/Hatch Open** (PID: 05B6) [cite: 229]
    * Eq: `(C:6-7)` | 1050ms | *Closed: 0x00, Driver: 0x80, Others: 0x40.*
* [cite_start]**Cruise Control Active** (PID: 05C8) [cite: 229]
    * Eq: `(C:4)` | 1050ms

---

## 5. Solicited (CAN) - Generic Engine (ECU 07E0)
* [cite_start]**Number of emissions DTCs** (PID: 0101) [cite: 229]
    * Eq: `(A:0-6)`
* [cite_start]**MIL Status** (PID: 0101) [cite: 229]
    * Eq: `(A:7)`
* [cite_start]**Engine Load (Torque)** (PID: 0104) [cite: 229]
    * Eq: `A` | Range: 0-100% (Peak torque 115Nm @ 4200)
* [cite_start]**ICE Temperature** (PID: 0105) [cite: 229]
    * Eq: `A-40` | Range: -40 to 215 °C
* [cite_start]**Short-Term Fuel Trim** (PID: 0106) [cite: 229]
    * Eq: `0.7812 * (A-128)` | Range: -100% to 99.22%
* [cite_start]**Long-Term Fuel Trim** (PID: 0107) [cite: 229]
    * Eq: `0.7812 * (A-128)` | Range: -100% to 99.22%
* [cite_start]**Vehicle Speed** (PID: 010D) [cite: 229]
    * Eq: `A` | Range: 0-255 km/hr
* [cite_start]**Timing Advance** (PID: 010E) [cite: 229]
    * Eq: `(A/2)-64` | Range: -64 to 63.5 (Relative to cylinder #1)
* [cite_start]**ICE RPM Actual** (PID: 010C) [cite: 229]
    * Eq: `(256*A+B)/4` | Range: 0-5200 rpm
* [cite_start]**Intake Air Temp** (PID: 010F) [cite: 229]
    * Eq: `A-40` | Range: -40 to 215 °C
* [cite_start]**MAF Air Flow** (PID: 0110) [cite: 229]
    * Eq: `(256*A+B)/100` | Range: 0-655.35 g/sec
* [cite_start]**Throttle Position** (PID: 0111) [cite: 229]
    * Eq: `(100*A)/255` | Range: 0-100%
* [cite_start]**Lambda** (PID: 0124) [cite: 229]
    * Eq: `0.0000305 * (256*A+B)`
* [cite_start]**O2 Sensor Output** (PID: 0124) [cite: 229]
    * Eq: `0.000122 * (256*C+D)` | Range: 0-8V
* [cite_start]**Odometer since DTC Clear** (PID: 0131) [cite: 229]
    * Eq: `256*A+B` | Range: 0-65535 Km
* [cite_start]**Barometric Pressure** (PID: 0133) [cite: 229]
    * Eq: `A` | Range: 0-255 kPa
* [cite_start]**Aux Batt Voltage (ECU Power)** (PID: 0142) [cite: 229]
    * Eq: `(256*A+B)/1000` | Range: 0-65.54 V
* [cite_start]**Ambient Air Temp** (PID: 0146) [cite: 229]
    * Eq: `A-40` | Range: -40 to 215 °C
* [cite_start]**Injector Time** (PID: 21F3) [cite: 229]
    * Eq: `0.128 * C` | Range: 0-32.64 ms

---

## 6. Solicited (CAN) - Hybrid/Specific (ECU 07E2)
[cite_start]*Ref: [cite: 232]*

* [cite_start]**MG2 Revolution (RPM)** (PID: 21C3) [cite: 232]
    * Eq: `((256*A)+B)-16383` | Range: -2000 to 7000 rpm
* [cite_start]**MG1 Revolution** (PID: 21C3) [cite: 232]
    * Eq: `((256*G)+H)-16383` | Range: -13000 to 13000 rpm
* [cite_start]**Engine Speed (Target)** (PID: 21C3) [cite: 232]
    * Eq: `(256*M)+N` | Range: 0-8000 rpm
* [cite_start]**Engine Speed (Actual ICE_RPM)** (PID: 21C3) [cite: 232]
    * Eq: `(256*O)+P` | Range: 0-8000 rpm
* [cite_start]**State of Charge** (PID: 21C3) [cite: 232]
    * Eq: `(100*S)/255` | Range: 40-80%
* [cite_start]**WOUT HV Batt to Converter** (PID: 21C3) [cite: 232]
    * Eq: `320 * T` | Range: 0-21 kW
* [cite_start]**WIN HV Batt to Converter** (PID: 21C3) [cite: 232]
    * Eq: `U - 40800` | Range: -25 to 0 kW
* [cite_start]**Discharge Request to Adjust SOC** (PID: 21C3) [cite: 232]
    * Eq: `V - 20480` | Range: -20480 to 20320 Watts
* [cite_start]**Drive Condition ID** (PID: 21C3) [cite: 232]
    * Eq: `X` | Range: 0-6
* [cite_start]**MG1 Inverter Temp** (PID: 21C3) [cite: 232]
    * Eq: `Y - 40` | Range: -40 to 215 °C
* [cite_start]**MG2 Inverter Temp** (PID: 21C3) [cite: 232]
    * Eq: `Z - 40` | Range: -40 to 215 °C
* [cite_start]**Motor Temp No2 (MG2)** (PID: 21C3) [cite: 232]
    * Eq: `AA - 40` | Range: -40 to 215 °C
* [cite_start]**Motor Temp No1 (MG1)** (PID: 21C3) [cite: 232]
    * Eq: `AB - 40` | Range: -40 to 215 °C
* [cite_start]**HV Power Resource VB (Voltage)** (PID: 21C3) [cite: 232]
    * Eq: `2 * AC` | Range: 150-300 V (Sum of cells)
* [cite_start]**HV Power Resource IB (Current)** (PID: 21C3) [cite: 232]
    * Eq: `2 * AE - 256` | Range: -100 to 100 A
* [cite_start]**Regen Brake Torque (Actual/Request)** (PID: 21C3) [cite: 232]
    * Eq: `4 * E` (Actual) or `4 * F` (Request) | Range: 0-186 Nm
* [cite_start]**Master Cylinder Torque** (PID: 21C3) [cite: 232]
    * Eq: `(4 * R) - 512` | Range: -512 to 508 Nm
* [cite_start]**MG2 Torque** (PID: 21C3) [cite: 232]
    * Eq: `(256*C+D)/8 - 500` | Range: -400 to 400 Nm
* [cite_start]**MG1 Torque** (PID: 21C3) [cite: 232]
    * Eq: `(256*I+J)/8 - 500` | Range: -200 to 200 Nm
* [cite_start]**Accelerator Pedal Angle** (PID: 21C4) [cite: 232]
    * Eq: `(100*C)/255` | Range: 0-100%
* [cite_start]**VL-Voltage Before Boosted** (PID: 21C4) [cite: 232]
    * Eq: `2 * D` | Range: 0-510 V
* [cite_start]**VH-Voltage After Boosted** (PID: 21C4) [cite: 232]
    * Eq: `2 * E` | Range: 0-765 V
* [cite_start]**Converter Temperature** (PID: 21C4) [cite: 232]
    * Eq: `F - 40` | Range: -40 to 215 °C
* [cite_start]**Crank Position** (PID: 21C4) [cite: 232]
    * Eq: `0.706 * G` | Range: 0-100
* [cite_start]**System Main Relay Status 1/2/3** (PID: 21C4) [cite: 232]
    * Eq: `(H:0)`, `(H:1)`, `(H:2)` (Binary)
* [cite_start]**Aircon Consumption Power** (PID: 21C4) [cite: 232]
    * Eq: `0.019608 * P` | Range: 0-5 kW
* [cite_start]**Requests (Engine Warm up, Aircon, Idle, etc)** (PID: 21C4) [cite: 232]
    * Various bitmasks on Bytes A and B.
* [cite_start]**Cruise Control Speed** (PID: 21C3) [cite: 232]
    * Set Speed: `B` (km/h) | Memory Speed: `A` (km/h). Resets to 0 < 40km/hr.

---

## 7. Solicited (CAN) - HV Battery (ECU 07E3)
[cite_start]*Ref: [cite: 233]*

* [cite_start]**HV Battery State of Charge** (PID: 21CE) [cite: 233]
    * Eq: `0.5 * A` | Range: 40-80%
* [cite_start]**HV Battery Current** (PID: 21CE) [cite: 233]
    * Eq: `(256*B+C)/100 - 327.68` | Range: -100 to 100 Amp
* [cite_start]**Battery Power** (PID: 21CE) [cite: 233]
    * Eq: `(256*D+E)/100 - 327.68` | Range: -27 to 27 kW
* [cite_start]**Block Voltages (Blocks 01-14)** (PID: 21CE) [cite: 233]
    * Equation Pattern: `(256*HighByte + LowByte)/100 - 327.68`
    * Range: 0-18 V
    * Note: Bytes pairs follow sequentially (F+G, H+I, etc.) through PID 21CE data.
* [cite_start]**HV Battery Air Intake Temp** (PID: 21CF) [cite: 233]
    * Eq: `(256*A+B)/100 - 327.68`
* [cite_start]**Auxiliary Battery Voltage** (PID: 21CF) [cite: 233]
    * Eq: `(0.2*D) - 25.6` | Range: 0-15 V
* [cite_start]**HV Battery Charge/Discharge Limit** (PID: 21CF) [cite: 233]
    * Charge: `E - 64` (kW)
    * Discharge: `F - 64` (kW)
* [cite_start]**Delta SOC** (PID: 21CF) [cite: 233]
    * Eq: `0.01 * G` | Range: 0-60%
* [cite_start]**HV Battery Fan Speed** (PID: 21CF) [cite: 233]
    * Range: 0-6
* [cite_start]**HV Battery Temps (1, 2, 3)** (PID: 21CF) [cite: 233]
    * Eq: `(256*ByteH + ByteL)/100 - 327.68`
* [cite_start]**Internal Resistance (R01 - R14)** (PID: 21D0) [cite: 233]
    * Eq: `0.001 * Byte` | Range: 0-10 Ohm
* [cite_start]**NiMH Volt Delta** (PID: 21D0) [cite: 233]
    * Eq: `(256*J + 0.01*N) - 327.68` | Range: 0-3 V
* [cite_start]**Block Voltage Min/Max Stats** (PID: 21D0) [cite: 233]
    * Includes Block # with Min/Max V and the voltage values.

---

## 8. Un-Solicited & Unknown (OBD2) - Raw List
[cite_start]*Ref: [cite: 234-238]*

* 0020 (Len 12)
* 0030 (Len 6)
* 0038, 003A, 003E (000000 B)
* 0084 (Len 14)
* 0230 (Len 28)
* 0262 (Len 21)
* 0348 (Len 44)
* 03C9 (Len 106)
* 03CD (Len 108)
* 03CF (Len 139)
* 0423 (Len 1075)
* 0484 (Len 1120)
* 04C1 (Len 971)
* 04C3 (Len 1042)
* 04C6 (Len 1018)
* 04C7 (Len 1028)
* 04C8 (Len 1058)
* 04CE (Len 1099)
* 04D0 (Len 1118)
* 04D1 (Len 1135)
* 0520 (Len 2231)
* 0521 (Len 322)
* 0527 (Len 1064)
* 0528 (Len 522)
* 0529 (Len 1032)
* 053F (Len 10647)
* 0540 (Len 1049)
* 0553 (Len 1042)
* 0554 (Len 1066)
* 0560 (Len 1069)
* 0591 (Len 336)
* 05B2 (Len 5268)
* 05CC (Len 272)
* 05D4 (Len 1073)
* 05EC (Len 526)
* 05ED (Len 1066)
* 05F8 (Len 1087)
* 0602 (Len 73009)