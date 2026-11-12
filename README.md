# NTP Stratum Simulation (Engineering TUI & Fault Injection)

Simulasi **Network Time Protocol (RFC 5905)** berbasis arsitektur **Hierarki Stratum (Stratum 1, 2, dan 3)** dengan **Terminal User Interface (TUI)** modern berstandar engineering, **Multi-Peer Cross Validation**, **Slew vs Step Dynamic Progress**, dan **Chaos Fault Injection** untuk tugas kelompok mata kuliah Sistem Terdistribusi (6 node/laptop).

![Language](https://img.shields.io/badge/Language-Python%203.8+-blue.svg)
![Interface](https://img.shields.io/badge/Interface-Rich%20TUI%20Dashboard-brightgreen.svg)
![Fault-Tolerance](https://img.shields.io/badge/Fault--Tolerance-Marzullo%20Algorithm-orange.svg)

---

## Fitur Utama

### 1. Freeze / Pause UI [SPACE]
- Tekan **`[SPACE]`** untuk membekukan tampilan antarmuka secara instan.
- **Proses sinkronisasi di latar belakang tetap berjalan normal** (server tetap melayani UDP, client tetap melakukan polling dan kalkulasi $\theta$ dan $\delta$).
- Sangat berguna saat presentasi di depan dosen untuk menghentikan pergerakan angka sementara dan mengamati perbandingan clock telemetry secara mendalam.
- Tekan **`[SPACE]`** kembali untuk melanjutkan rendering live.

### 2. Dynamic Brownian Hardware Drift
- Tekan **`[D]`** untuk mengaktifkan simulasi osilator kuarsa fisik dinamis (random walk / thermal fluctuation).
- Nilai drift tidak konstan, melainkan bergerak dinamis naik dan turun (antara $-25\text{ ms/s}$ hingga $+25\text{ ms/s}$):
  `[DRIFT: +14.2ms/s FAST]` -> `[DRIFT: +8.5ms/s FAST]` -> `[DRIFT: -3.1ms/s SLOW]`

### 3. Perbedaan Nyata Slew vs Step Mode
- **`STEP` (Instant Jump):** Jam langsung melompat seketika ke waktu baru.
- **`SLEW` (Gradual Adaptation):** Jam tidak melompat (menjaga konsistensi sistem file & DB). Jam beradaptasi secara gradual ($\pm 0.40\text{ s/s}$) lengkap dengan **Progress Bar**:
  `[SLEWING SMOOTHLY] Left: +3.200s / Total: +14.200s  [████████████░░░░░░] 77%`

### 4. Falseticker: Manual vs Auto-Chaos
- **`[F]` - Manual Falseticker:** Menyuntikkan distorsi waktu $+60.0$ detik secara konstan. Layar membelah menampilkan jam asli vs jam palsu yang disiarkan.
- **`[C]` - Auto Chaos Mode:** Mengaktifkan gangguan acak sporadis (peluang 35% di tiap request menghasilkan lonjakan skew acak $\pm 30\text{s}$ s.d. $\pm 90\text{s}$).

### 5. Deteksi IP Lokal Otomatis & Visual Topology
- Header langsung menampilkan **Alamat IP LAN laptop**:
  `IP: 192.168.1.45:5000 (hostname: laptop-bagas)`
- Diagram hierarki Stratum visual dan grafik sparkline Unicode (`█▆▅▃▂     `) untuk memantau kurva konvergensi waktu.

---

## Hotkeys Interaktif

| Tombol | Fungsi | Keterangan |
| :---: | :--- | :--- |
| **`[SPACE]`** | **Freeze/Pause UI** | Membekukan telemetry di layar tanpa menghentikan proses background. |
| **`[F]`** | **Manual Falseticker** | Menyuntikkan kesalahan waktu $+60$ detik secara sengaja. |
| **`[C]`** | **Auto-Chaos Mode** | Mengaktifkan gangguan waktu acak sporadis (35% probability). |
| **`[M]`** | **Slew vs Step** | Beralih antara lompatan instan (`STEP`) atau penyesuaian halus (`SLEW`). |
| **`[D]`** | **Dynamic Drift** | Mengaktifkan/menonaktifkan drift acak dinamis ($\pm\text{ms/s}$). |
| **`[Q]`** | **Quit** | Keluar dari aplikasi dengan bersih. |

---

## Instalasi & Persiapan

```bash
git clone https://github.com/jempolbagas/ntp-simulation.git
cd ntp-simulation
pip install -r requirements.txt
```

---

## Panduan Menjalankan

### Skenario Demo 6 Laptop (Wi-Fi / LAN / Tailscale)

Catat IP Laptop 1, 2, dan 3 yang tampil di header masing-masing laptop:
- Misal IP Laptop 1 (Stratum 1): `192.168.1.10`
- Misal IP Laptop 2 (Stratum 2A): `192.168.1.11`
- Misal IP Laptop 3 (Stratum 2B): `192.168.1.12`

Jalankan di masing-masing laptop:

| Laptop | Peran | Perintah Eksekusi |
| :--- | :--- | :--- |
| **Laptop 1** | **Stratum 1 (Master)** | `python3 ntp_node.py --stratum 1 --port 5000` |
| **Laptop 2** | **Stratum 2A (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 3** | **Stratum 2B (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 4** | **Stratum 3 (Client 1)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 5** | **Stratum 3 (Client 2)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 6** | **Stratum 3 (Client 3)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
