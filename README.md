# ⏱️ NTP Stratum Simulation (Interactive TUI with Fault Injection)

Simulasi **Network Time Protocol (RFC 5905)** berbasis arsitektur **Hierarki Stratum (Stratum 1, 2, dan 3)** dengan **Interactive Terminal User Interface (TUI)** modern, **Multi-Peer Cross Validation**, dan **Live Fault Injection** untuk tugas kelompok mata kuliah Sistem Terdistribusi (6 node/laptop).

![Language](https://img.shields.io/badge/Language-Python%203.8+-blue.svg)
![Interface](https://img.shields.io/badge/Interface-Rich%20TUI%20Dashboard-brightgreen.svg)
![Fault-Tolerance](https://img.shields.io/badge/Fault--Tolerance-Marzullo%20Algorithm-orange.svg)

---

## ✨ Fitur Baru & Interaktivitas Live

### 1. 🎮 Hotkeys Interaktif (Tanpa Perlu Restart Program)
Cukup tekan tombol di keyboard saat program sedang berjalan di terminal:
- **`[F]` - Toggle Falseticker (Fault Injection):**
  - Mengubah node menjadi **server bohong / rusak** yang menyuntikkan distorsi waktu $+60.0$ detik ke semua client yang meminta waktu.
- **`[M]` - Toggle Sync Mode (Slew vs Step):**
  - **`STEP`:** Jam langsung melompat instan ke waktu baru.
  - **`SLEW`:** Jam tidak boleh melompat mendadak! Offset disesuaikan secara gradual dan halus (slewing) untuk menjaga konsistensi urutan log database.
- **`[D]` - Toggle Hardware Drift:**
  - Mengaktifkan simulasi ketidaksempurnaan osilator kuarsa fisik (jam otomatis meleset $+5\text{ ms}$ tiap detik) untuk membuktikan pentingnya sinkronisasi berkala.
- **`[Q]` - Quit:**
  - Menghentikan node dengan aman dan bersih.

---

### 2. 🛡️ Multi-Upstream & Falseticker Rejection (Marzullo's Concept)
Stratum 3 sekarang dapat memantau **beberapa server Stratum 2 sekaligus**:
- Jika kedua server Stratum 2 mengirimkan waktu yang konsisten $\rightarrow$ Keduanya diklasifikasikan sebagai `● TRUECHIMER` dan sistem memilih server dengan delay terkecil (*Clock Filter*).
- Jika salah satu Stratum 2 menekan tombol `[F]` (menjadi Falseticker) $\rightarrow$ Client Stratum 3 otomatis mendeteksi outlier tersebut, menandainya sebagai `⚠️ FALSETICKER (DISCARDED)`, dan jam client **tetap aman** karena hanya menyinkronkan ke server yang jujur!

---

## 🚀 Instalasi & Persiapan

Clone repository ini ke masing-masing laptop:
```bash
git clone https://github.com/jempolbagas/ntp-simulation.git
cd ntp-simulation
pip install -r requirements.txt
```

---

## 💻 Panduan Menjalankan

### 1. Uji Coba Mandiri di 1 Laptop (Localhost)
Buka 4 terminal berbeda untuk menguji skenario Falseticker:

- **Terminal 1 (Stratum 1 - Master Server):**
  ```bash
  python3 ntp_node.py --stratum 1 --port 5001
  ```
- **Terminal 2 (Stratum 2A - Relay Jujur):**
  ```bash
  python3 ntp_node.py --stratum 2 --port 5002 --upstream 127.0.0.1:5001
  ```
- **Terminal 3 (Stratum 2B - Calon Falseticker):**
  ```bash
  python3 ntp_node.py --stratum 2 --port 5003 --upstream 127.0.0.1:5001
  ```
- **Terminal 4 (Stratum 3 - Client dengan Multi-Upstream):**
  ```bash
  python3 ntp_node.py --stratum 3 --upstream 127.0.0.1:5002 127.0.0.1:5003
  ```

> **Skenario Uji Coba:**
> 1. Perhatikan Terminal 4 menampilkan kedua server sebagai `● TRUECHIMER`.
> 2. Pindah ke Terminal 3, tekan tombol **`F`**. Terminal 3 akan memunculkan badge merah `⚠️ FALSETICKER MODE ACTIVE`.
> 3. Lihat Terminal 4! Server 5003 otomatis berubah menjadi `⚠️ FALSETICKER (DISCARDED)`, dan jam Terminal 4 tetap sinkron mengikuti Server 5002.
> 4. Tekan tombol **`M`** di Terminal 4 untuk melihat jarum jam bergerak gradual (mode SLEW).

---

### 2. Skenario Demo 6 Laptop (Wi-Fi / LAN / Tailscale)

Misalkan IP:
- Laptop 1 (Stratum 1): `192.168.1.10`
- Laptop 2 (Stratum 2A): `192.168.1.11`
- Laptop 3 (Stratum 2B): `192.168.1.12`

Jalankan di masing-masing laptop:

| Laptop | Peran | Perintah Eksekusi |
| :--- | :--- | :--- |
| **Laptop 1** | **Stratum 1 (Master)** | `python3 ntp_node.py --stratum 1 --port 5000` |
| **Laptop 2** | **Stratum 2A (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 3** | **Stratum 2B (Relay)** | `python3 ntp_node.py --stratum 2 --port 5000 --upstream 192.168.1.10:5000` |
| **Laptop 4** | **Stratum 3 (Client 1)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 5** | **Stratum 3 (Client 2)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |
| **Laptop 6** | **Stratum 3 (Client 3)** | `python3 ntp_node.py --stratum 3 --upstream 192.168.1.11:5000 192.168.1.12:5000` |

---

## 📐 Rumus Matematis NTP

$$\delta = (T_4 - T_1) - (T_3 - T_2) \quad \text{(Round-Trip Delay)}$$

$$\theta = \frac{(T_2 - T_1) + (T_3 - T_4)}{2} \quad \text{(Clock Offset)}$$

- **Step Mode:** $Waktu_{\text{baru}} = Waktu_{\text{saat\_ini}} + \theta$
- **Slew Mode:** Penyesuaian bertahap $\pm 0.25\text{ s/s}$ hingga target $\theta$ tercapai.
