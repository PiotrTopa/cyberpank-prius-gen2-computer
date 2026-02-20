Prius Gen2 (2009) CAN/OBD2 PID Codes
Following is a list of CAN/OBD2 PID codes which have been working with a Gen2
2009 Prius. This list is not original, but an incomplete compilation of the work of
others who are gratefully acknowledged; in-particular Atilla Vass and
usbseawolf2000. These codes are for directly monitoring the Prius CAN-bus using
the following hardware which is given here for information only (not association,
recommendation or otherwise). An Arduino CAN-bus shield is connected to the
Prius OBD2 connector, and fitted to an Arduino Mega2560 which also connects to
a display, GPS and relay board, and logs data to a µSD card.
Mega2560 firmware is based on the CAN-bus shield/Arduino library expanded to
parse (Prius Gen2) ISO 15765-2 CAN multi-frame messages. This library
communicates with CAN using a message structure comprised of an unsigned
integer (2-byte) ID, a (data length) header byte, and 8 bytes of data. (The
nomenclature may not be correct, but) the list refers to “OBD2” messages, where
the message ID is comprised of Mode and PID code, and “CAN” messages, where
the message ID is the source ECU address (eg. 0x07E0, elsewhere referred to as
the Header), and Mode and PID code are embedded in the message data bytes.
OBD2 messages are the (un-solicited) messages broadcast by Prius ECU’s during
their normal activity (~1500/sec); CAN messages are the (solicited) generic or
Prius-specific requests for data sent to an ECU.
Any corrections, additions (particularly regarding unknown Prius OBD2
messages), etc. are most welcome.
MSB LSB Header A B C D E F G H
ID
(unsigned int)
Header
(byte)
Data
(byte[8])
Bits 7..5 Bits 3..0
Data
Length
Bit 4
rtr
CAN-Shield Library Message Structure:
Mode PID Header A B C D E F G H
Bits 7..5 Bits 3..0
Data
Length
Bit 4
0
OBD2 Message:
ECU Header Frame
Info Mode PID A B C D E
CAN (SF) Single-Frame Message:
Bits 7..4
SF Frame
Type: 0x0
Bits 3..0
CAN Data
Length
Bits 15..0
Bit 3 Set in Reply
1
Bits 5..0
Mode
Bit 6 Set
1
ECU Header Mode PID A B C D
CAN (FF) First-Frame (of multi-frame) Message:
Bits 15..12
FF Frame
Type: 0x1
CAN Data
Length
Bits 15..0
Bit 3 Set in Reply
1
Bits 5..0
Mode
Bit 6 Set
1
Bits 11..0
MCU CAN-Bus
MCU CAN-Bus
MCU CAN-Bus
ECU Header CAN Tx
Len: 0x02 Mode PID 0 0 0 0 0
CAN PID Request Message:
MCU CAN-Bus
ECU Header Frame
Info
Block
Size: 0x00
Min Sep.
Time: 0x00 0 0 0 0 0
CAN (FC) Flow Control (further frames) Request Message:
MCU CAN-Bus
CAN (CF) Consecutive-Frames (of multi-frame) Message:
MCU CAN-Bus ECU Header Frame
Info E,L, F,M, G,N, H,O, I,P, J,Q, K,R,etc.
Bits 7..4
CF Frame
Type: 0x2
Bits 3..0
Frame Seq #
(starting 1)
Bits 15..0
Bit 3 Set in Reply
1
Frame Info
(2 bytes)
Bits 7..4
FC Frame
Type: 0x3
Bits 3..0
Flow Control
Flag: 0x0
eg. Car Speed 0x03 0xCA A B C D E
Bits 7..5 Bits 3..0
Data
Length
Bit 4
0
Bits 7..5 Bits 3..0
Data
Length
Bit 4
0
Bits 7..5 Bits 3..0
Data
Length
Bit 4
0
Bits 7..5 Bits 3..0
Data Len
0x8
Bit 4
0
Bits 7..5 Bits 3..0
Data
Length
Bit 4
0
0 0x5
Request with SF reply: eg. Car Speed 0x07E0 0 0x8 2 0x01 0x0D 0 0 0 0 0
Request with FF/CF reply: eg. Hybrid Info 0x07E2 0 0x8 2 0x21 0xC3 0 0 0 0 0
eg. Car Speed (SF) 0x07E8 0 0x8 0x0 0x41 0x0D A
eg. Hybrid Info (FF) 0x07EA 0 0x8 0x027 0x61 0xC4 A B C D
eg. Hybrid Info (FC) 0x07E2 0 0x8 0x00 0x00 0 0 0 0 0
eg. Hybrid Info (CF1) 0x07EA 0 0x8 E F G H I J K
eg. Hybrid Info (CF2) 0x07EA 0 0x8 L M N O P Q R
0x3
0x1
0x3 0x0
0x2 0x1
0x2 0x2
:
Arduino CAN-Shield / CAN-bus Message Exchange
Conventions - #: decimal, 0x# hexidecimal (one # per 4-bit nibble)
: additional CF's as required to pass message data length
ref: Automated Cross-Platform Reverse Engineering of CAN Bus Commands From Mobile Apps.pdf
ECU Description
0x727 Transmission
0x750 Main Body
0x780 AirBag
0x781 Precrash
0x790 Distance
0x791 Precrash2
0x7A1 Steering Assist
0x7A2 Park Assist
0x7B0 ABS Brake
0x7C0 Instrument
0x7C4 Air Conditioner
0x7D0 Navigation
0x7E0 Engine Controls
0x7E2 Hybrid System
Description ECU
Mode
&PID Equation
~Update
(mS) Min Max Units Notes (Toyota Prius Gen2 2009, ref: https://en.wikipedia.org/wiki/Toyota_Prius_(XW20))
Un-Solicited (OBD2), Prius2 Specific
Lateral acceleration 0022 (256 * A + B) - 0x0200 13 Un'Cal Units unknown
Longitudinal acceleration 0023 (256 * A + B) - 0x0200 13 Un'Cal Units unknown
Steering angle 0025 (256 * A + B) 13 Un'Cal 12bit signed integer, "straight-ahead" value vehicle specific
Brake Pedal Position 0030 E 6 0 127 Un'Cal Value may be 0x7F (maybe slightly -ve) when not-pressed or on start-up with brake pedal depressed for READY
ICE Temperature 0039 A 8 0 255 °C Same result as (solicited) 07E00105, though can't go negative. Use 07E00105 as this unnecessarily rapid
EM Amp 003B (256 * A + B) / 10 8 Amp 12bit signed integer. Use 07E221C3, same + additional info
EM Volt 003B D 8 0 255 Volt Nominal 201V
Front right wheel pulses 00B1 256 * A + B 13 0 65535 Pulses Measured ~185 pulses/revolution, seems odd encoder rate ?
Front left wheel pulses 00B1 256 * C + D 13 0 65535 Pulses
Rear right wheel pulses 00B3 256 * A + B 13 0 65535 Pulses Measured ~185 pulses/revolution, seems odd encoder rate ?
Rear left wheel pulses 00B3 256 * C + D 13 0 65535 Pulses
Drive Mode 0120 {D:0-1} 17 0 3 Binary P: 0x0, R: 0x1, N: 0x2, D: 0x3. Nb: differs from A.Vass
Throttle pedal position 0244 G 25 0 0xC8 Un'Cal Use 07E221C4, same + additional info
ICE Speed (Target) 03C8 256 * C + D 70 0 65535 RPM Target not actual. Use 07E221C3, same + additional info
Vehicle speed 03CA C 108 0 255 km/hr
HV SOC % 03CB (256 * C + D)/2 108 0 100 % Nb: MFD SOC displays 40%-80% of SOC range (refer 00000529{D:0-2} for MFD SOC bars displayed)
HV Max Discharge Current 03CB A 108 0 255 Amp Nominally 100A <51°C (ref: https://www.eaa-phev.org/wiki/Prius_PHEV_TechInfo#Battery_ECU_messages)
HV Max Charge Current 03CB B 108 0 255 Amp Nominally 105A <51°C (ref: https://www.eaa-phev.org/wiki/Prius_PHEV_TechInfo#Battery_ECU_messages)
Ignition Timing Advance 0526 B,C ? 530 Un'Cal Use 07E0010E
0x00000529 Msg Event 0529 {A:7} 1000* 0 1 Binary * Event occurred causing immediate 00000529 msg; By default 00000529 issued every 1sec (ref: https://www.eaa-phev.org/wiki/Prius_PHEV_TechInfo)
General Problem, with Triangle 0529 {B:2,4,6} 0 1 Binary 1 beep, red triangle
Not in Park and Drivers Door is Open 0529 {B:3} 0 1 Binary
General Problem 0529 {B:5} 0 1 Binary 1 beep
General Problem 0529 {C:0} 0 1 Binary 1 beep
General Problem, with Triangle 0529 {C:1} 0 1 Binary 1 beep, red triangle
MFD SOC Bars displayed (40-80%) 0529 {D:0-2} 0 7 Number bars depends on SOC % (~40%-80%) and whether HV charging/discharging (re: https://www.eaa-phev.org/wiki/Toyota_Prius_Battery_Specs)
Brake Pedal Depressed 0529 {D:3} 0 1 Binary
EV-Mode Active 0529 {E:6} 0 1 Binary
EV-Mode Cancelled 0529 {E:7} 0 1 Binary 1 beep
Parking Brake Problem 0529 {F:4} 0 1 Binary MFD: "There's a problem with the transmission 'P' lock mechanism. Park you car on a flat surface, and fully apply the parking brake."
EV-Mode Denied 0529 {F:5,6,7} 0 1 Binary MFD: "Cannot change to EV mode now"
ICE Temperature 052C B / 2 1050 0 127 °C Use 07E00105
Headlights Status 057F {B:3-5} 1050 Binary Off: 0x00, Parkers: 0x10, Low-Beam: 0x30, High-Beam: 0x38
Fuel Tank Level 05A4 B 3400 0 ~0x2C litres? Measured full tank 0x2C/44, assume calibration in litres
Doors/Hatch Open 05B6 {C:6-7} 1050 Binary Closed: 0x00, Drivers Door Open: 0x80, Other Doors/Hatch Open: 0x40
Cruise Control Active 05C8 {C:4} 1050 Binary Cruise control On and Active
Solicited (CAN), Generic (ECU 07E0)
PID's Supported (0101 - 0120) 07E0 0100 {A:7}-{D:0} Binary Supported Mode 01 PID's: 0x01,0x03,0x04,0x05,0x06,0x07,0x0C,0x0D,0x0E,0x0F,0x10,0x11,0x13,0x15,0x1C,0x1F,0x20
Number of emissions-related DTC's 07E0 0101 {A:0-6} ref: https://en.wikipedia.org/wiki/OBD-II_PIDs#Service_01_PID_01
MIL Status 07E0 0101 {A:7} Binary MIL light off/on
Engine Type 07E0 0101 {B:3} Binary 0: Spark ignition monitors supported (e.g. Otto or Wankel engines), 1: Compression ignition monitors supported (e.g. Diesel engines)
Calc'd Engine Load (Torque) 07E0 0104 A 0 100 % Indicates a percentage of peak available torque, 115Nm @ 4200
ICE Temperature 07E0 0105 A - 40 -40 215 °C Same result as (un-solicited) 00000039
Short-Term Fuel Trim 07E0 0106 0.7812 * (A - 128) -100 99.22 % -100%(Rich) to 99.22%(Lean)
Long-Term Fuel Trim 07E0 0107 0.7812 * (A - 128) -100 99.22 % -100%(Rich) to 99.22%(Lean)
Vehicle speed 07E0 010D A 0 255 km/hr
Timing Advance 07E0 010E (A / 2) - 64 -64 63.5 ° Relative to cylinder #1
ICE RPM Actual 07E0 010C (256 * A + B) / 4 0 5200 rpm
Intake Air Temperature 07E0 010F A - 40 -40 215 °C
MAF Air Flow Rate 07E0 0110 (256 * A + B) / 100 0 655.35 g/sec
Throttle pedal position 07E0 0111 (100 * A) / 255 0 100 %
PID's Supported (0121 - 0140) 07E0 0120 {A:7}-{D:0} Binary Supported Mode 01 PID's: 0x21,0x24,0x30,0x31,0x33,0x3C,0x3E,0x40
Lambda 07E0 0124 0.0000305 * (256 * A + B) 0 2 Lambda value
O2 Sensor Output 07E0 0124 0.000122 * (256 * C + D) 0 8 Volt O2 (voltage) sensor output
Odometer since DTC's Last Cleared 07E0 0131 256 * A + B 0 65535 Km
Barometric Pressure 07E0 0133 A 0 255 kPa
PID's Supported (0141 - 0160) 07E0 0140 {A:7}-{D:0} Binary Supported Mode 01 PID's: 0x42,0x43,0x44,0x45,0x46,0x47,0x4C,0x4D,0x4E
ECU Power Voltage (Aux Batt Voltage) 07E0 0142 (256 * A + B) / 1000 0 65.54 Volt
Ambient Air Temperature 07E0 0146 A - 40 -40 215 °C
Injector Time 07E0 21F3 0.128 * C 0 32.64 mS ref: https://priuschat.com/threads/gen2-prius-custom-pids-for-torque-android-app-with-formulas.95370/page-8#post-1415782
Solicited (CAN), Prius2 Specific
ICE Temperature 07E0 0105 A - 40 -40 215 °C Same result as (un-solicited) 00000039
ICE Actual rpm 07E0 010C (256 * A + B) / 4 0 5200 rpm
Timing Advance 07E0 010E (A / 2) - 64 -64 63.5 °
Lambda 07E0 0124 0.0000305 * (256 * A + B) 0 2
O2 Sensor Output 07E0 0124 0.000122 * (256 * C + D) 0 8 Volt O2 (voltage) sensor output
MG2 Revolution (MG2_RPM) 07E2 21C3 ((256 * A) + B) - 16383 -2000 7000 rpm
MG1 Revolution 07E2 21C3 ((256 * G) + H) - 16383 -13000 13000 rpm
Engine Speed (Target) 07E2 21C3 (256 * M) + N 0 8000 rpm Not much interest
Engine Speed (Actual, ICE_RPM) 07E2 21C3 (256 * O) + P 0 8000 rpm
State Of Charge 07E2 21C3 (100 * S) / 255 40 80 % Same result as (un-solicited) 0x000003CB (Vass conversion SOC = 0.5*S)
WOUT HV Batt to Converter 07E2 21C3 320 * T 0 21 kW
WIN HV Batt to Converter 07E2 21C3 U - 40800 -25 0 kW
Discharge Request to Adjust SOC 07E2 21C3 V - 20480 -20480 20320 Watt
Drive Condition ID 07E2 21C3 X 0 6 Num
MG1 Inverter Temperature 07E2 21C3 Y - 40 -40 215 °C
MG2 Inverter Temperature 07E2 21C3 Z - 40 -40 215 °C
Motor Temperature No2 (MG2 ?) 07E2 21C3 AA - 40 -40 215 °C
Motor Temperature No1 (MG1 ?) 07E2 21C3 AB - 40 -40 215 °C
HV Power Resource VB 07E2 21C3 2 * AC 150 300 Volt HV battery (sum cells) voltage
HV Power Resource IB 07E2 21C3 2 * AE - 256 -100 100 Amp HV battery current
Regenerative Brake Torque (Actual, ReGen_T) 07E2 21C3 4 * E 0 186 Nm Equals MG2 torque when MG2 regen braking (ie. negative) else zero. Doesn't show MG2 doing simulated engine braking (ie. off throttle but not on brake).
Regenerative Brake Torque (Request) 07E2 21C3 4 * F 0 186 Nm Much the same as Regenerative Brake Torque (Actual)
Master Cylinder Torque 07E2 21C3 (4 * R) - 512 -512 508 Nm Seems to be Total braking effort (units of torque) being Regen braking (MG2) + friction braking. Nb: Raw value behaves like a signed 7bit value. ReGen_T same
as MG2_T when +ve, though MG2_2 shows "engine braking" component of regen.
(ICE) Power Request 07E2 21C3 ((256 * K) + L) / 100 0 320 kW Nb: ~Linear with ICE RPM * (115Nm * Calc'd Engine Load (07E00104)) with ~3kW intercept
MG2 Torque (MG2_T) 07E2 21C3 (256 * C + D) / 8 - 500 -400 400 Nm
MG1 Torque (MG1_T) 07E2 21C3 (256 * I + J) / 8 - 500 -200 200 Nm
Shift Sensor Main 07E2 21C3 0.019608 * AF 0 5 Volt
Shift Sensor Sub 07E2 21C3 0.019608 * AG 0 5 Volt
Shift Sensor Select Main 07E2 21C3 0.019608 * AH 0 5 Volt
Shift Sensor Select Sub 07E2 21C3 0.019608 * AI 0 5 Volt
Shift Sensor Shift Position 07E2 21C3 0.019608 * AJ 0 5 Num
Accelerator Pedal Angle 07E2 21C4 (100 * C) / 255 0 100 %
VL-Voltage Before Boosted 07E2 21C4 2 * D 0 510 Volt
VH-Voltage After Boosted 07E2 21C4 2 * E 0 765 Volt
Converter Temperature 07E2 21C4 F - 40 -40 215 °C
Crank Position 07E2 21C4 0.706 * G 0 100 °
System Main Relay 1 Status 07E2 21C4 {H:0} 0 1 Bin
System Main Relay 2 Status 07E2 21C4 {H:1} 0 1 Bin
System Main Relay 3 Status 07E2 21C4 {H:2} 0 1 Bin
Converter Carrier Frequency 07E2 21C4 {I:0} 0 1 Bin
Smart Key Status 07E2 21C4 {I:2} 0 1 Bin
Aircon Gate Status 07E2 21C4 {I:4} 0 1 Bin
Converter Gate Status 07E2 21C4 {I:5} 0 1 Bin
MG2 Gate Status 07E2 21C4 {I:6} 0 1 Bin
MG1 Gate Status 07E2 21C4 {I:7} 0 1 Bin
Motor (MG2) Torque Execute Value 07E2 21C4 4 * J - 512 -512 508 Nm
Motor (MG1) Torque Execute Value 07E2 21C4 4 * K - 512 -512 508 Nm
Short Circuit Wave Highest Value 07E2 21C4 0.019608 * L 0 5 Volt
Raising Pressure Ratio 07E2 21C4 (100 * O) / 255 0 100 %
Aircon Consumption Power 07E2 21C4 0.019608 * P 0 5 kW
Driving Pattern 1 07E2 21C4 {A:0} 0 1 Bin
Driving Pattern 2 07E2 21C4 {A:1} 0 1 Bin
Driving Pattern 3 07E2 21C4 {A:2} 0 1 Bin
Loading Condition 07E2 21C4 {A:7} 0 1 Bin
Engine Warming Up Request 07E2 21C4 {B:0} 0 1 Bin
Aircon Request 07E2 21C4 {B:1} 0 1 Bin
Engine Stop Inhibit Request 07E2 21C4 {B:2} 0 1 Bin
HVAC OBD Request 07E2 21C4 {B:3} 0 1 Bin
Main Battery Charging Request 07E2 21C4 {B:4} 0 1 Bin
Engine Idling Request 07E2 21C4 {B:5} 0 1 Bin
Engine Stop Request 07E2 21C4 {B:6} 0 1 Bin
Check Mode 07E2 21C4 {B:7} 0 1 Bin
Cruise Control Memory Vehicle SPD 07E2 21D3 A 0 150 km/h Vehicle speed (used by cruise control)
Cruise Set/Resume Speed 07E2 21D3 B 0 255 km/h Resets to 0 when speed < ~40km/hr. Originally ref'd as "Cruise Throttle Opening Angle"
Cruise Control Main Switch -- (Main CPU) 07E2 21D3 {C:0} 0 1 Bin
Cruise Control Main Switch -- Ready (Main CPU) 07E2 21D3 {C:2} 0 1 Bin
Cruise Control Main Switch -- Indicator (Main CPU)07E2 21D3 {C:5} 0 1 Bin
Cruise Control 07E2 21D3 {C:6} 0 1 Bin
Shift D Position 07E2 21D3 {C:7} 0 1 Bin
Stop Light Switch 1 (Sub CPU) 07E2 21D3 {D:0} 0 1 Bin
Stop Light Switch 2 (Sub CPU) 07E2 21D3 {D:1} 0 1 Bin
Stop Light Switch 1 (Main CPU) 07E2 21D3 {D:2} 0 1 Bin
RES / ACC Switch 07E2 21D3 {D:3} 0 1 Bin
SET / COAST Switch 07E2 21D3 {D:4} 0 1 Bin
Cancel Switch 07E2 21D3 {D:5} 0 1 Bin
Cruise Control Active (Set) 07E2 21D3 {D:6} 0 1 Bin Cruise control active. Use with care as seems to occassionaly give 0 while cruise active for a short period (<1sec).
D drive mode selected 07E2 21D3 {D:7} 0 1 Bin Originally ref'd as "Cruise Control Enabled"
HV Battery State of Charge 07E3 21CE 0.5 * A 40 80 %
HV Battery Current 07E3 21CE (256 * B + C) / 100 - 327.68 -100 100 Amp
Battery power (kW) 07E3 21CE HV Battery Current * Σ Blocks Volts -27 27 kW
HV Battery Block-01 Voltage 07E3 21CE (256 * D + E) / 100 - 327.68 0 18 Volt
HV Battery Block-02 Voltage 07E3 21CE (256 * F + G) / 100 - 327.68 0 18 Volt
HV Battery Block-03 Voltage 07E3 21CE (256 * H + I) / 100 - 327.68 0 18 Volt
HV Battery Block-04 Voltage 07E3 21CE (256 * J + K) / 100 - 327.68 0 18 Volt
HV Battery Block-05 Voltage 07E3 21CE (256 * L + M) / 100 - 327.68 0 18 Volt
HV Battery Block-06 Voltage 07E3 21CE (256 * N + O) / 100 - 327.68 0 18 Volt
HV Battery Block-07 Voltage 07E3 21CE (256 * P + Q) / 100 - 327.68 0 18 Volt
HV Battery Block-08 Voltage 07E3 21CE (256 * R + S) / 100 - 327.68 0 18 Volt
HV Battery Block-09 Voltage 07E3 21CE (256 * T + U) / 100 - 327.68 0 18 Volt
HV Battery Block-10 Voltage 07E3 21CE (256 * V + W) / 100 - 327.68 0 18 Volt
HV Battery Block-11 Voltage 07E3 21CE (256 * X + Y) / 100 - 327.68 0 18 Volt
HV Battery Block-12 Voltage 07E3 21CE (256 * Z + AA) / 100 - 327.68 0 18 Volt
HV Battery Block-13 Voltage 07E3 21CE (256 * AB + AC) / 100 - 327.68 0 18 Volt
HV Battery Block-14 Voltage 07E3 21CE (256 * AD + AE) / 100 - 327.68 0 18 Volt
HV Battery Air Intake Temp 07E3 21CF (256 * A + B) / 100 - 327.68 -327.7 350 °C
VMF Fan Motor Voltage 07E3 21CF (0.2 * C) - 25.6 9 12 Volt
Auxiliary Battery Voltage 07E3 21CF (0.2 * D) - 25.6 0 15 Volt
HV Battery Charge 07E3 21CF E - 64 0 50 kW
HV Battery Discharge 07E3 21CF F - 64 0 50 kW
Delta SOC 07E3 21CF 0.01 * G 0 60 %
HV Battery Fan Speed 07E3 21CF I 0 6 Num
HV Battery Temp 1 07E3 21CF (256 * K + L) / 100 - 327.68 -327.7 350 °C
HV Battery Temp 2 07E3 21CF (256 * M + N) / 100 - 327.68 -327.7 350 °C
HV Battery Temp 3 07E3 21CF (256 * O + P) / 100 - 327.68 -327.7 350 °C
Internal Resistance R01 07E3 21D0 0.001 * P 0 10 Ohm
Internal Resistance R02 07E3 21D0 0.001 * Q 0 10 Ohm
Internal Resistance R03 07E3 21D0 0.001 * R 0 10 Ohm
Internal Resistance R04 07E3 21D0 0.001 * S 0 10 Ohm
Internal Resistance R05 07E3 21D0 0.001 * T 0 10 Ohm
Internal Resistance R06 07E3 21D0 0.001 * U 0 10 Ohm
Internal Resistance R07 07E3 21D0 0.001 * V 0 10 Ohm
Internal Resistance R08 07E3 21D0 0.001 * W 0 10 Ohm
Internal Resistance R09 07E3 21D0 0.001 * X 0 10 Ohm
Internal Resistance R10 07E3 21D0 0.001 * Y 0 10 Ohm
Internal Resistance R11 07E3 21D0 0.001 * Z 0 10 Ohm
Internal Resistance R12 07E3 21D0 0.001 * AA 0 10 Ohm
Internal Resistance R13 07E3 21D0 0.001 * AB 0 10 Ohm
Internal Resistance R14 07E3 21D0 0.001 * AC 0 10 Ohm
HV Battery Block Count 07E3 21D0 A 0 14 Num
Accumulated Time of Battery LOW 07E3 21D0 (256 * B) + C 0 5000 Sec
Accumulated Time of DC Inhibit 07E3 21D0 (256 * D) + E 0 5000 Sec
Accumulated Time of Battery Too High 07E3 21D0 (256 * F) + G 0 5000 Sec
Accumulated Time of Hot Temperature 07E3 21D0 (256 * H) + I 0 5000 Sec
NiMH Volt Delta 07E3 21D0 ((2.56 * M) + (0.01 * N) - 327.68) - ((2.56 * J) + (0.01 * K) - 327.68) -3 3 Volt
HV Battery Block Lowest Volt 07E3 21D0 (2.56 * J) + (0.01 * K) - 327.68 0 15 Volt
HV Battery Block # with Min V 07E3 21D0 L 0 13 Num
HV Battery Block Highest Volt 07E3 21D0 (2.56 * M) + (0.01 * N) - 327.68 0 23 Volt
HV Battery Block # with Max V 07E3 21D0 O 0 13 Num
Un-Solicited & Unknown (OBD2), Prius2 Specific
0020 12
0030 6
0038 8
003A 8
003E 8
00B4 14
0230 28
0262 21
0348 44
03C9 106
03CD 108
03CF 139
0423 1075
0484 1120
04C1 971
04C3 1042
04C6 1018
04C7 1028
04C8 1058
04CE 1099
04D0 1118
04D1 1135
0520 2231
0521 322
0527 1064
0528 522
0529 1032
053F 10647
0540 1049
0553 1042
0554 1066
056D 1069
0591 336
05B2 5268
05CC 272
05D4 1073
05EC 526
05ED 1066
05F8 1087
0602 73009