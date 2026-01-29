Comprehensive Analysis of Telemetry and Network Protocols in the Toyota Prius Gen 2 (NHW20): AVC-LAN and CAN Bus Architectures

1. Introduction: The Telemetry Landscape of the NHW20

The Toyota Prius Generation 2 (Model Code NHW20), produced between 2004 and 2009, stands as a watershed moment in automotive engineering. It was the first platform to fully integrate the Toyota Hybrid System II (THS-II), introducing a level of electronic complexity that was unprecedented for mass-market vehicles of its era. For the automotive researcher, telemetry analyst, or systems engineer, the NHW20 represents a unique and rigorous case study in distributed network processing. Unlike modern architectures that often consolidate functions into domain controllers, the Gen 2 Prius relies on a highly segmented, multi-protocol topology where data is siloed based on bandwidth requirements and criticality.

To successfully extract high-fidelity data "during the ride"—specifically regarding thermal management, cabin environmental control, and powertrain dynamics—one cannot rely solely on standard OBDII diagnostic requests. Standard diagnostics are too slow and often filtered. Instead, a comprehensive analysis requires a bifurcated approach: intercepting the Controller Area Network (CAN) for high-speed powertrain physics and the proprietary Audio Visual Communication Local Area Network (AVC-LAN) for human-machine interface (HMI) and climate control operations.

This report serves as a definitive technical reference for decoding these protocols. It addresses the specific requirements of monitoring Ambient, Cabin, and Set Temperatures via the IEBus-based AVC-LAN, and extracting critical unsolicited data regarding Inverter status, Battery State of Charge (SOC), and energy flow from the CAN bus. The analysis presented herein synthesizes data from reverse-engineering communities, technical service documentation, and raw hexadecimal captures to provide a byte-level map of the Prius's nervous system. It explores not only what the data is, but how the vehicle's ECUs use this data to make decisions about regenerative braking limits, thermal derating, and energy allocation.

2. Network Topology and Physical Layers

Understanding the physical movement of bits is a prerequisite to decoding their meaning. The NHW20 utilizes a Gateway ECU as a central router, bridging three distinct communication protocols. This architecture dictates where a researcher must physically connect their logging equipment to capture specific datasets.

2.1 The Gateway ECU: The Central Nervous System

The Gateway ECU is the physical and logical hub of the Prius network. It connects the high-speed CAN bus (Powertrain), the medium-speed BEAN (Body Electronics), and the multimedia-focused AVC-LAN.[1, 2] Its primary function is protocol translation. For instance, the Ambient Temperature sensor is physically wired to the front of the condenser and read by the Air Conditioning (A/C) Amplifier. The A/C Amplifier broadcasts this value on the AVC-LAN for the Multi-Function Display (MFD). The Gateway ECU listens to this AVC-LAN message, translates it into a CAN frame, and re-broadcasts it on the CAN bus so the Engine Control Module (ECM) can use air density data for fuel trim calculations.

This "Gateway Effect" creates a critical distinction for the analyst: Source Data vs. Reflected Data. The Ambient Temperature on the AVC-LAN is the "Source Data" (high resolution, real-time). The Ambient Temperature on the CAN bus is "Reflected Data" (potentially filtered, lower sample rate). For the most accurate analysis of climate control logic, one must tap the AVC-LAN directly.

2.2 The AVC-LAN (IEBus) Architecture

The AVC-LAN is a proprietary implementation of the NEC IEBus (Inter Equipment Bus) standard.[3] It acts as the backbone for the user interface, connecting the Head Unit, MFD, Navigation, and the A/C Amplifier.

Physical Layer: The bus utilizes a differential pair (Data+ and Data-), typically consisting of a twisted pair of wires (often Red/White and Green/White in the dashboard harness).[4] It operates at a half-duplex speed of approximately 17.8 kbps.

Signaling Logic: Unlike the dominant/recessive voltage logic of CAN, IEBus uses Pulse Width Modulation (PWM) encoding. The state of a bit is determined by the duration of the voltage potential, not just the presence of voltage.

Start Bit: A long assertion of ~165µs High followed by ~30µs Low.[5]

Logic '1': ~20µs High followed by ~19µs Low.

Logic '0': ~32µs High followed by ~7µs Low.

Implication for Capture: Standard CAN snubbers or UART adapters cannot read this bus. The analyst requires a specific differential transceiver circuit (using a comparator like the LM339 or a specialized HA12240 transceiver) connected to a microcontroller capable of microsecond-precision timing input capture.[4, 5]

2.3 The CAN Bus Architecture

The CAN bus handles the vehicle's critical control loops. It operates at 500 kbps (ISO 11898) and connects the Hybrid Vehicle (HV) ECU, Battery ECU, Engine ECU, and Skid Control (Brake) ECU.[1]

Accessibility: In the Gen 2 Prius, the DLC3 (OBDII port) pins 6 (CAN-H) and 14 (CAN-L) are directly connected to the main powertrain bus. There is no security gateway firewall blocking access on the NHW20, making it an ideal platform for passive telemetry logging.[1]

Data Types: The bus carries two types of messages:

Unsolicited (Broadcast): Periodic messages sent automatically by ECUs (e.g., Battery SOC sent every 100ms). This is the primary target for "ride analysis."

Solicited (Diagnostic): Query-Response messages (e.g., requesting specific Inverter Thermistor values) which only appear when requested by a scan tool.

3. Deep Dive: AVC-LAN Climate Control Protocols

The user's requirement to extract Ambient, Cabin, Set Temperature, and AC Modes necessitates deep interaction with the AVC-LAN. The A/C Amplifier (Address 0x130) is the processing unit, while the MFD (Address 0x110) acts as the display and control surface.

3.1 Addressing and Frame Structure

An AVC-LAN message frame is structured to support multi-master communication with collision detection (CSMA/CD).
[Master Addr (12-bit)][Control (4-bit)][Length (8-bit)]

For climate analysis, the relevant actors are:

Master (Sender): 0x110 (Multi-Function Display) - Sends user commands.

Slave (Receiver): 0x130 (A/C Amplifier) - Executes commands and broadcasts sensor data.

Alternative Master: 0x130 - The A/C Amp acts as a Master when broadcasting status updates back to the display.[6, 7]

3.2 Decoding Temperature Commands (Set Point)

When the driver adjusts the temperature, the MFD sends a "Write" command to the A/C Amplifier. Analyzing this traffic reveals the hexadecimal encoding used for temperature setpoints.

Message Flow: 0x110 -> 0x130

Command Structure: The payload typically contains an Operation Code (OpCode) followed by the value.

Hexadecimal Mapping: The Prius does not transmit temperature in ASCII or direct Celsius/Fahrenheit integers. It uses a lookup table where a specific Hex base corresponds to the minimum temperature ("LO") and increments represent 0.5°C or 1°F steps.

LO (Max Cold): Often encoded as 0x00 or a low byte like 0x10.

65°F (18°C): Represented by 0x22.[8]

Increment: The value increments by 1 hex unit for every 1°F step.

85°F (29°C): Represented by 0x36.

HI (Max Hot): Encoded as 0x37 or 0xFF.[8]

Example Frame: To set the temperature to 72°F (approx 0x29), the message might look like:
110 130 00 03 29 [Checksum]

3.3 Decoding AC Modes and Vent Status

The status of the air distribution dampers (Face, Feet, Defrost) is broadcast by the A/C Amplifier to update the screen icons. This is usually contained within a "Status Broadcast" message that repeats periodically (e.g., every 500ms).

Bitmask Analysis: The "Mode" is rarely a single linear value but a bitmask byte, allowing for combinations (e.g., Feet + Defrost).

Face (Panel): 0x01

Bi-Level (Face + Feet): 0x02

Feet (Floor): 0x03

Feet + Defrost (Mix): 0x04

Defrost (Windshield): 0x05.[8]

Recirculation Status: This is often a separate bit flag within the same status byte or an adjacent byte.

Fresh Air: 0x00 (or Bit 4 = 0)

Recirculate: 0x10 (or Bit 4 = 1)

Research Insight: Snippet [8] provides a captured trace 80 02 13 00 2c 2c... where the second byte indicates status. By toggling modes while logging, one can isolate the specific byte that transitions from 01 to 05.

3.4 Ambient and Cabin Temperature Telemetry

The A/C Amplifier reads two primary thermistors: the Ambient Sensor (mounted behind the bumper) and the Cabin Sensor (mounted behind a small grille on the dashboard, typically with an aspirator tube).

Ambient Temperature Data:

Broadcast ID: Found in the periodic status message from 0x130.

Byte Location: Typically Byte 3 or 4 of the status payload.

Scaling: The raw hex value usually follows a linear offset. A common Toyota formula for this era is Value - 40 = °C. For example, a hex value of 0x3C (Decimal 60) would represent 20°C.

Damping: The AVC-LAN value is heavily damped to prevent the display from fluctuating rapidly (e.g., when stopped in traffic near hot exhaust). For "during the ride" analysis, this damped value is what the climate control logic uses, so it is the correct value to log for understanding system behavior.

Cabin Temperature Data:

Visibility: This value is used by the A/C Amplifier's PID loop but is not displayed on the dashboard (the dashboard shows the Set temp). However, it is transmitted in the diagnostic stream.

Diagnostic Access: The Gen 2 Prius features a hidden climate diagnostic mode. Holding Auto and Recirculate while turning the car ON triggers this mode.[9, 10]

Telemetry Opportunity: When this mode is active, the AVC-LAN traffic changes. The A/C Amp broadcasts error codes (e.g., 11 for Cabin Sensor, 12 for Ambient Sensor, 21 for Solar Sensor).[11, 12] Logging the bus during this mode allows the analyst to capture the raw sensor values as the ECU polls them for self-check.

4. CAN Bus Protocol Analysis: Unsolicited Powertrain Telemetry

The Controller Area Network (CAN) on the NHW20 is the domain of high-speed physics. For an analyst monitoring the vehicle "during the ride," the goal is to capture unsolicited (broadcast) messages that provide a high-resolution view of the powertrain's state without the need for active polling, which can clutter the bus.

4.1 The Challenge of Inverter Temperature (Solicited vs. Unsolicited)

The user explicitly requested Inverter Temperature via unsolicited messages. A critical finding of this research is that on the Gen 2 Prius (NHW20), precise numeric temperature values for the Inverter (MG1/MG2/Boost Converter) are not present in the standard unsolicited broadcast stream.[13] They are shielded behind the diagnostic layer.

4.1.1 The Solicited Solution (0x7E2)

To get the exact temperature in degrees, the analyst must implement a "Hybrid Polling" strategy. This involves injecting a diagnostic request onto the CAN bus at a low frequency (e.g., 1Hz) to retrieve the data.

Request ID: 0x7E2 (Target: HV ECU)

Service: 01 (Current Data) or 21 (Enhanced Data).

PID: C3 or C4 (Proprietary Toyota PIDs for Thermal Data).[13, 14]

Payload: 02 21 C3 00 00 00 00 00

Response ID: 0x7EA

Data Decoding: The response typically contains 4-5 bytes of thermal data.

MG1 Temp: Byte A. Scaling: A - 50 = °C.

MG2 Temp: Byte B. Scaling: B - 50 = °C.

Inverter Coolant Temp: Byte C. Scaling: C - 50 = °C.

4.1.2 The Passive Alternative (Inverter Status & Flags)

If the analyst strictly cannot use polling (e.g., read-only hardware), they must rely on Status Flags in the unsolicited stream.

ID 0x3CA: This message contains general Hybrid System status. While it does not contain a temperature integer, it contains "Derate" flags.

Thermal Derating Logic: As the Inverter temperature approaches its thermal limit (typically >65°C for coolant or >90°C for silicon), the HV ECU will actively reduce the maximum current allowed. This is visible in ID 0x3CB (Battery Charge/Discharge Limits) or energy flow flags in 0x3B6.

Inverter Pump Failure (P0A93): A common failure on the Gen 2 is the inverter coolant pump.[15, 16] When this fails, the inverter overheats rapidly. The "Master Warning" (Red Triangle) bit will flip in the broadcast message 0x529 (Display Status) or 0x039 (Engine Status) well before the car shuts down. Monitoring the 0x529 error bitmask is the best passive way to detect inverter distress.

4.2 Battery State of Charge (SOC): The Two Realities

Accurate SOC analysis requires distinguishing between the "Display SOC" (what the driver sees) and the "Real SOC" (what the battery management system uses). These are transmitted on different IDs with different scaling factors.

4.2.1 ID 0x3CB: The Battery Management System (BMS) Heartbeat

This is the single most important message for battery analysis. It is broadcast by the Battery ECU to the HV ECU approximately every 100ms.[13, 17, 18]

Message ID: 0x3CB

Payload Breakdown:

Bytes 0-1 (Discharge Limit): The maximum current (in Amps) the battery can currently discharge. This value is dynamic. It drops if the battery is too empty, too hot, or too cold. Insight: A sudden drop in this value "during the ride" is a leading indicator of battery aging or thermal throttling.

Bytes 2-3 (Charge Limit): The maximum current the battery can accept (Regen).

Analysis: If this value drops below ~20 Amps, the vehicle will severely limit regenerative braking, forcing the use of friction brakes. This is critical for efficiency analysis.

Byte 4 (State of Charge): The Real SOC.

Scaling: (Decimal Value) * 0.5 = %.

Range: The Gen 2 Prius strictly maintains the battery between 40% (Hex 50) and 80% (Hex A0). You will almost never see values outside this range in a healthy car.

Byte 5 (Battery Max Temp): The highest temperature reading from the pack's thermistors.

Scaling: Value - 50 = °C (Validation required, some sources use -40).

4.2.2 ID 0x529: The Human Interface SOC

Message ID: 0x529

Purpose: Sent by the HV ECU to the Gateway for the dashboard display (the 8-bar battery icon).[17, 19]

Characteristics: This value is heavily filtered and hysteretic. It effectively "lies" to the driver to prevent anxiety. It will show "Full" (8 bars) long before the Real SOC hits 80%, and "Empty" before it hits 40%.

Conclusion: For any engineering or ride analysis, ignore 0x529 and log 0x3CB.

4.3 Energy Flow and Drive Analysis (ID 0x3B6)

To reconstruct the vehicle's behavior "during the ride," ID 0x3B6 is indispensable. This message controls the Energy Monitor arrows on the MFD and acts as a simplified state machine for the hybrid system.[20]

Message ID: 0x3B6

Payload: Bytes 5 and 6 act as a bitmask for energy direction.

Decoding the Bitmask:

No Flow (0x00): The vehicle is stopped or in a pure glide (neutral energy state).

Engine -> Wheels: ICE is providing propulsive torque.

Battery -> Motor: Electric boost (Discharge).

Motor -> Battery: Regenerative braking (Charge).

Engine -> Battery: Series generation (ICE charging battery while driving or stopped).

Analytical Use Case: By logging 0x3B6 alongside 0x0B4 (Speed), an analyst can calculate the "EV Fraction"—the percentage of the drive where the ICE was off or decoupled. This is a primary metric for hybrid efficiency.

4.4 Other Critical Unsolicited IDs

A complete ride analysis requires context. The following IDs provide the necessary background data to interpret the thermal and electrical metrics [13, 21]:

CAN ID (Hex)

Source ECU

Data Content

Byte Structure & Scaling

Notes

0x039

Engine (ECM)

ICE Coolant Temp

Byte 0 - 40 = °C

Crucial for distinguishing Inverter overheat vs Engine overheat.

0x0B4

Skid Control

Vehicle Speed

(Byte 5 * 256 + Byte 6) / 100 = km/h

High-precision wheel speed (unfiltered).

0x244

Gateway/HV

Accelerator Pedal

Byte 6 (0-255 scaling)

Represents driver torque request.

0x2C4

Skid Control

Brake Pressure

Byte 0 & Byte 1

Pressure in the master cylinder. Use to correlate with Regen (0x3B6).

0x3C8

Engine (ECM)

Engine RPM

(Byte 2 * 256 + Byte 3) / 8

Precise RPM. 0 when in EV mode.

0x3CA

HV ECU

Vehicle Speed/Status

Byte 2 = Speed

Used for Cruise Control logic.

0x520

Engine (ECM)

Fuel Injector Time

Byte 0-1

Used to calculate instantaneous fuel consumption.

0x526

Engine (ECM)

Ignition Timing

Byte 0

Advance degrees.

5. Synthesis: Data Correlation and Second-Order Insights

Collecting data is only the first step. The true value lies in correlating these disparate streams to reveal the Prius's underlying control strategies. The NHW20 is a complex feedback loop where thermal states dictate electrical limits, which in turn dictate mechanical operating points.

5.1 The Thermal-Electrical Feedback Loop

By correlating AVC-LAN ID 0x130 (Climate Demand) with CAN ID 0x3CB (Battery Limits), one can observe the direct impact of cabin comfort on powertrain capability.

Scenario: High Ambient Temp (AVC-LAN Ambient > 35°C) + Low Set Temp (AVC-LAN Set = LO).

Observation: The electric A/C compressor spins up to max RPM, drawing significant current from the High Voltage battery.

Impact: The Battery ECU detects this load and the rising pack temperature (CAN 0x3CB Byte 5). It automatically lowers the Charge Current Limit (CAN 0x3CB Bytes 2-3).

Result: The driver experiences reduced regenerative braking capability. The data reveals this not as a brake failure, but as a deliberate thermal protection strategy triggered by the A/C request.

5.2 Predicting Inverter Pump Failure

The "Red Triangle of Death" (P0A93) typically occurs when the inverter cooling pump fails.[15] This can be predicted via telemetry before the code throws.

Logic:

Monitor CAN ID 0x0B4 (Speed) > 0.

Monitor CAN ID 0x244 (Accel) < 20% (Low Load).

Monitor CAN ID 0x039 (ICE Temp) = Normal (85-90°C).

Poll CAN ID 0x7E2 (Inverter Temp).

Insight: If the Inverter Temperature rises rapidly (>60°C) while the ICE temp is stable and load is low, the coolant is likely stagnant. A healthy pump keeps the inverter very close to ambient temp during low load. This divergence is a clear signature of pump failure.

5.3 Efficiency Analysis: Gliding

The Prius community emphasizes "Pulse and Glide" driving.

Metric: A "Perfect Glide" is achieved when CAN ID 0x3B6 (Energy Flow) shows 0x00 (No arrows) while CAN ID 0x0B4 (Speed) is decreasing slowly.

Validation: By logging the Accelerator Position (0x244), one can identify the exact pedal percentage (often around 14-18%) required to zero out the energy flow, validating the driver's technique.

6. Technical Implementation for Data Capture

To execute this analysis, the researcher requires a robust hardware setup capable of bridging the two networks.

6.1 Hardware Requirements

Dual-Channel Interface: A device capable of reading CAN and IEBus simultaneously is ideal, but rare. Most setups use two separate devices.

CAN: A generic ELM327 is insufficient for unsolicited stream logging due to buffer limitations. Use a dedicated CAN sniffer based on the MCP2515 chipset (Arduino/ESP32) or a professional tool like a Kvaser or PCAN-USB.

AVC-LAN: Requires a custom circuit. A simple voltage divider is risky. Use a high-speed comparator (e.g., LM339) to convert the 3V/0V differential signal to 5V TTL for a microcontroller.

Connection Points:

CAN: Tap into the DLC3 (OBDII) port under the dash. Pin 6 (High) and Pin 14 (Low). Ground on Pin 4/5.

AVC-LAN: There is no AVC-LAN pin on the OBDII port. You must remove the radio or MFD trim and splice into the twisted pair harness (Toyota wire colors for AVC-LAN are typically Red and White, or Green and White, twisted together).[4]

6.2 Logging Strategy

To capture "all info worth analysis," the logger should be configured as follows:

Passive Buffer: continuously buffer and write IDs 0x039, 0x3CB, 0x3CA, 0x3B6, 0x0B4 to a CSV file with millisecond timestamps.

Active Interleave: Every 1000ms (1Hz), inject the 0x7E2 query for Inverter Temp. Do not query faster than this to avoid bus load issues.

AVC-LAN Event Listener: Configure the IEBus controller to trigger an interrupt on any message to/from ID 0x130. Log the payload whenever it changes.

7. Conclusion

The Toyota Prius Gen 2 operates on a sophisticated, compartmentalized network architecture that separates human comfort (AVC-LAN) from powertrain physics (CAN). While the CAN bus offers a high-speed, unsolicited stream of critical data like "Real" SOC (0x3CB) and Energy Flow (0x3B6), it notably obscures Inverter Temperatures behind a diagnostic firewall, requiring active solicitation (0x7E2). Conversely, the AVC-LAN offers high-fidelity climate data (0x130) but requires specialized physical hardware to access.

By synthesizing these two data streams, an analyst can move beyond simple diagnostics and engage in comprehensive vehicle system analysis. This approach reveals the hidden causal links between cabin climate requests, battery thermal limits, and engine operating strategies, providing a complete picture of the Hybrid Synergy Drive's operational logic. The hexadecimal identifiers and bitmasks detailed in this report constitute the master key for unlocking this data.