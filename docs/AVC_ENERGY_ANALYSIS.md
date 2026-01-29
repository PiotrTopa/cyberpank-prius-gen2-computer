# AVC-LAN Energy Monitor Data Analysis Summary

## Analiza danych energetycznych w AVC-LAN - Toyota Prius Gen 2

### Stan wiedzy

#### Co wiemy z CAN bus:
| CAN ID | Dane | Format |
|--------|------|--------|
| 0x3C8 | HV Battery SOC | Bajt 4 = SOC surowy (0-200 = 0-100%) |
| 0x3CB | HV Battery Power | Bajty 0-1 = Moc (signed, kW) |
| 0x3CD | HV Battery Current | TBD |
| 0x3CF | HV Battery Voltage | TBD |
| 0x030 | Engine Status | Stan ICE |

#### Co wiemy z AVC-LAN:

**Status ICE (silnik spalinowy) - ZNALEZIONY:**
```
Źródło: 210 -> 490
Format: 00 46 XX 00
Bajt[2]: C1 = ICE OFF, C8 = ICE RUNNING
```

**Inne źródła statusu:**
- `110->490` - EMV/MFD status (najczęstszy)
- `B10->490` - nieznane źródło
- `D10->490` - nieznane źródło
- `112->060` - alternatywny kanał MFD

### Problem

MFD (Multi Function Display) pokazuje:
1. **Battery SOC %** - poziom naładowania baterii HV
2. **Power Flow Diagram** - przepływ energii (silnik ↔ bateria ↔ koła)
3. **Fuel Consumption** - zużycie paliwa
4. **Regeneration Indicator** - odzyskiwanie energii

MFD NIE ma bezpośredniego połączenia z CAN - wszystko przychodzi przez AVC-LAN.

### Hipotezy

#### 1. Dane Energy Monitor mogą być w dużych pakietach (32 bajty)

**Kandydaci:**
- `A00->258` - 35 pakietów, 32 bajty każdy
- `C10->028` - 10 pakietów, 32 bajty
- `806->C48` - 7 pakietów, 32 bajty

Struktura `A00->258`:
```
Bajty [0-7]:  02 01 05 00 20 31 02 00  <- Stały nagłówek
Bajty [8-15]: 04 00 00 00 XX XX XX XX  <- XX = zmienny
Bajty [16-23]: 00 XX XX XX XX XX XX XX <- Bardzo zmienne
Bajty [24-31]: XX XX XX XX XX XX XX XX <- Zmienne
```

#### 2. Energy data może być zakodowana w `110->490` (8-bajtowe wersje)

Format: `00 00 00 XX XX XX XX XX`
- Bajty 3-7 są bardzo zmienne
- Mogą zawierać zakodowane SOC/Power

#### 3. Brak wystarczających danych

Nasze nagrania są krótkie (~30-60 sekund) i mogą nie zawierać pełnego
zakresu zmian SOC. SOC zmienia się powoli (1% na kilka minut jazdy).

### Rekomendacje dla dalszych badań

1. **Dłuższe nagranie AVC-LAN** podczas jazdy (15-30 minut)
   - Obserwować zmiany SOC na MFD
   - Nagrywać jednocześnie CAN (dla korelacji)

2. **Korelacja czasowa** - porównać timestamps:
   - Kiedy CAN 0x3C8 zmienia SOC
   - Jakie wiadomości AVC-LAN pojawiają się w tym czasie

3. **Stymulacja MFD** - przełączać tryby wyświetlania:
   - CONSUMPTION
   - ENERGY MONITOR
   - Obserwować które wiadomości AVC-LAN reagują

4. **Analiza `002->660`** - pakiety kontroli wyświetlacza:
   - 16-32 bajty
   - Mogą zawierać komendy rysowania diagramu energii

### Struktura potencjalnych danych Energy Monitor

Na podstawie analizy, Energy Monitor może działać tak:

```
[Hybrid ECU] --CAN--> [Gateway/EMV] --AVC-LAN--> [MFD Display]
                                |
                         tłumaczy dane
                         CAN -> AVC-LAN format
```

Podejrzewam, że:
- `210` lub `A00` to źródło danych z Hybrid ECU
- Dane są enkodowane w specyficznym formacie AVC-LAN
- MFD (adres `110/112`) przekazuje status do `490`

### Następne kroki w kodzie

1. ~~Dodać dekoder dla `A00->258` pakietów~~ ✅ ZROBIONE
2. ~~Obserwować korelację `210->490` z CAN 0x3C8~~ ✅ ZROBIONE
3. Nagrać dłuższą sesję z pełnym cyklem ładowania/rozładowania baterii

### Developer Analysis Mode

Dodano tryb analizy (klawisz `A` podczas replay) który loguje:

```
[NRG] [seq] A00->258 (32B): HEX_DATA...
      Variable bytes[12+]: HEX_DATA...

[ICE] [seq] 210->490 ICE=OFF/RUNNING: HEX_DATA...

[BTN] [seq] PRESS: button_name (code=0xXXXX, mod=0xXX, suffix=0xXX)
      Raw: HEX_DATA...

[TCH] [seq] TOUCH/DRAG/RELEASE: x=XXX, y=YYY (conf=N)
      Raw: HEX_DATA...
```

Użycie:
```bash
python -m cyberpunk_computer --replay assets/data/avc_lan.ndjson --dev --scale 1
# Naciśnij 'A' aby włączyć tryb analizy
# Naciśnij 'V' aby włączyć verbose logging
```
