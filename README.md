# ⏱️ NTP Stratum Simulation (Interactive Terminal Dashboard)

Simulasi **Network Time Protocol (RFC 5905)** berbasis arsitektur **Hierarki Stratum (Stratum 1, 2, dan 3)** dengan **Interactive Terminal User Interface (TUI)** modern untuk tugas kelompok mata kuliah Sistem Terdistribusi (6 node/laptop).

![Language](https://img.shields.io/badge/Language-Python%203.8+-blue.svg)
![UI](https://img.shields.io/badge/Interface-Rich%20TUI%20Dashboard-brightgreen.svg)
![License](https://img.shields.io/badge/License-MIT-purple.svg)

---

## ✨ Fitur Visual & Fungsional Utama

- **🖥️ Live Interactive TUI Dashboard (Powered by `rich`):**
  - **Header Status:** Stratum Badge berwarna, info Upstream, status Socket Server, dan indikator sinkronisasi (`● SYNCHRONIZED` / `○ CONVERGING`).
  - **Side-by-Side Dual Clocks:** Menampilkan **OS System Clock** vs **Simulated NTP Clock** yang berdetik live secara real-time.
  - **Dynamic Offset / Drift Gauge:** Indikator visual horizontal `[-30s ───[■]─── +30s]` yang bergeser dinamis ke titik tengah `[ 0.000 ms ]` saat jam terkoreksi.
  - **NTP Mathematical Deduction Card:** Menampilkan 4 timestamps aktual ($T_1, T_2, T_3, T_4$) serta hasil kalkulasi Round-Trip Delay ($\delta$) dan Clock Offset ($\theta$).
  - **Convergence History Table:** Log riwayat sinkronisasi 5 putaran terakhir.
  - **Inbound Client Monitor (Stratum 1 & 2):** Menampilkan daftar client yang meminta sinkronisasi ke node ini.

- **🎲 Random Initial Clock Drift:**
  - **Stratum 1 (Root Master):** Terkunci di offset `0.000s` sebagai sumber kebenaran absolut.
  - **Stratum 2 & 3:** Otomatis menghasilkan offset awal acak (`-30s` s.d. `+30s`) saat dijalankan agar proses konvergensi jam terlihat jelas saat presentasi ke dosen.

- **🛡️ Fallback Mode:**
  - Menyediakan flag `--no-ui` untuk lingkungan terminal terbatas atau piping output log teks biasa.

---

## 🚀 Instalasi & Persiapan

Clone repository ini ke masing-masing laptop anggota:
```bash
git clone https://github.com/jempolbagas/ntp-simulation.git
cd ntp-simulation
```

Install dependensi visual `rich`:
```bash
pip install -r requirements.txt
```

---

## 💻 Panduan Menjalankan

### 1. Uji Coba Mandiri di 1 Laptop (Localhost)
Buka 3 terminal berbeda:

- **Terminal 1 (Stratum 1 - Master Server):**
  ```bash
  python3 ntp_node.py --stratum 1 --port 5001
  ```
- **Terminal 2 (Stratum 2 - Relay Node):**
  ```bash
  python3 ntp_node.py --stratum 2 --port 5002 --upstream 127.0.0.1:5001
  ```
- **Terminal 3 (Stratum 3 - Leaf Client):**
  ```bash
  python3 ntp_node.py --stratum 3 --upstream 127.0.0.1:5002
  ```

---

### 2. Skenario Demo 6 Laptop (Wi-Fi / LAN / Tailscale)

Pastikan semua laptop terhubung ke jaringan yang sama dan catat IP lokal Laptop 1, 2, dan 3:
- Misal IP Laptop 1 (Stratum 1): `192.168.1.10`
- Misal IP Laptop 2 (Stratum 2): `192.168.1.11`
- Misal IP Laptop 3 (Stratum 2): `192.168.1.12`

Jalankan perintah berikut di masing-masing laptop:

| Laptop | Peran | Perintah Eksekusi |
| :--- | :--- | :--- |
| **Laptop 1** | **Stratum 1 (Master)** | `python3 ntp_node.py --stratum 1 --port 5000` |
| **Laptop 2** | **Stratum 2 (Relay A)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 3** | **Stratum 2 (Relay B)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 4** | **Stratum 3 (Client A1)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000` |
| **Laptop 5** | **Stratum 3 (Client A2)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000` |
| **Laptop 6** | **Stratum 3 (Client B1)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.12:5000` |

> **Catatan Firewall:** Pastikan port UDP `5000` diizinkan di firewall Windows/macOS pada Laptop 1, 2, dan 3 agar paket request dari stratum di bawahnya tidak terblokir.

---

## 📐 Rumus Matematis NTP yang Diimplementasikan

$$\text{Round-Trip Delay } (\delta) = (T_4 - T_1) - (T_3 - T_2)$$

$$\text{Clock Offset } (\theta) = \frac{(T_2 - T_1) + (T_3 - T_4)}{2}$$

$$\text{Koreksi Waktu: } \text{VirtualClock}_{\text{new}} = \text{VirtualClock}_{\text{current}} + \theta$$
