# NTP Stratum Simulation

Simulasi **Network Time Protocol (NTP)** berbasis hierarki **Stratum (1, 2, dan 3)** menggunakan Python UDP socket untuk tugas mata kuliah Sistem Terdistribusi (6 node/laptop).

## Fitur Utama
- **Zero External Dependencies:** Hanya menggunakan library standar Python (`socket`, `json`, `threading`, `random`, `argparse`, `time`, `datetime`).
- **Virtual Clock & Random Initial Offset:**
  - **Stratum 1 (Master):** Offset awal terkunci di `0.000s`.
  - **Stratum 2 & 3:** Otomatis men-generate offset awal acak (`-30s` s.d. `-5s` atau `+5s` s.d. `+30s`) agar penyimpangan dan proses sinkronisasi terlihat jelas saat demo.
- **Perhitungan NTP Standar (4 Timestamps):**
  - Mencatat $T_1$ (Client Transmit), $T_2$ (Server Receive), $T_3$ (Server Transmit), dan $T_4$ (Client Receive).
  - Menghitung Round-Trip Delay ($\delta = (T_4 - T_1) - (T_3 - T_2)$) dan Clock Offset ($\theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$).

## Cara Menjalankan

### 1. Uji Coba Lokal (1 Laptop, 3 Terminal)
- **Terminal 1 (Stratum 1 - Master):**
  ```bash
  python3 ntp_node.py --stratum 1 --port 5001
  ```
- **Terminal 2 (Stratum 2 - Relay):**
  ```bash
  python3 ntp_node.py --stratum 2 --port 5002 --upstream 127.0.0.1:5001
  ```
- **Terminal 3 (Stratum 3 - Client):**
  ```bash
  python3 ntp_node.py --stratum 3 --upstream 127.0.0.1:5002
  ```

### 2. Simulasi 6 Laptop (Jaringan Wi-Fi / LAN / Tailscale)
Misal:
- IP Laptop 1 (Stratum 1): `192.168.1.10`
- IP Laptop 2 (Stratum 2): `192.168.1.11`
- IP Laptop 3 (Stratum 2): `192.168.1.12`

Jalankan di masing-masing laptop:
- **Laptop 1 (Stratum 1):** `python3 ntp_node.py --stratum 1 --port 5000`
- **Laptop 2 (Stratum 2):** `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000`
- **Laptop 3 (Stratum 2):** `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000`
- **Laptop 4 (Stratum 3):** `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000`
- **Laptop 5 (Stratum 3):** `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000`
- **Laptop 6 (Stratum 3):** `python3 ntp_node.py --stratum 3 --upstream 192.168.1.12:5000`
