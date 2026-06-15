# ChimeraStick

Turn any USB pendrive into a portable, globally accessible personal server. Once booted on any computer, it hosts a secure, high-performance web file manager accessible worldwide via an automated Cloudflare HTTPS tunnel, backed by a premium Apple-inspired dark interface.

---

## 1. System Architecture

When you write ChimeraStick to a USB drive using our installer, it partitions the flash memory into three distinct segments:

```
USB Drive Partition Layout:
├─ Partition 1 (200 MiB)   : FAT32, EFI System Partition (ESP) + GRUB Bootloader
├─ Partition 2 (512 MiB)   : FAT32 (Windows) / ext4 (Linux), holds boot kernel + compressed RootFS
└─ Partition 3 (remainder) : exFAT, labeled "CHIMERA_DATA", stores persistent configuration and user uploads
```

- **Boot Flow**: The host computer's firmware loads the GRUB bootloader from Partition 1. GRUB mounts Partition 2 and loads the Linux kernel (`vmlinuz-lts`) and initial ramdisk (`initramfs-lts`) into RAM.
- **Operating System**: A customized, immutable Alpine Linux distribution loads. The entire root OS partition is a compressed read-only SquashFS file system (`rootfs.squashfs`). Any runtime modifications are mapped to a temporary RAM overlay (tmpfs). This eliminates OS corruption from abrupt shutdowns and minimizes write wear on your flash memory.
- **Persistence**: User files, configuration settings, and administrator credentials are encrypted and stored inside the `CHIMERA_DATA` exFAT partition.
- **Web Application Stack**: Fronted by a highly optimized Nginx reverse proxy serving static assets directly, with Flask/Waitress running the administration dashboard and file explorer on port `8080`.
- **Global Access**: Enables an on-demand Cloudflare Tunnel client (`cloudflared`) to bypass local firewalls and NAT configurations, producing a temporary secure HTTPS URL (e.g., `https://example.trycloudflare.com`) accessible from anywhere.

---

## 2. Project Directory Structure

```
ChimeraStick/
├── payload/                   # Prepared files copied directly onto the USB
│   └── boot/
│       └── grub.cfg           # GRUB configuration with UUID templates
├── src/
│   ├── chimera-server/        # Python Flask web server administration application
│   │   ├── static/            # Apple-style glassmorphic stylesheets
│   │   ├── templates/         # HTML structure files using SVG Lucide icons
│   │   ├── app.py             # Server code containing resource monitoring
│   │   └── requirements.txt   # Python dependency list
│   ├── installer/
│   │   ├── windows/           # Windows Go USB installer (PowerShell + Diskpart integration)
│   │   └── linux/             # Linux shell USB installer (parted + tar integration)
│   └── build/                 # OS rootfs environment build scripts
│       ├── nginx.conf         # Highly optimized server configurations
│       ├── chimera-server.init # OpenRC supervision daemon startup config
│       └── build_rootfs.sh    # Script to construct and package Alpine SquashFS root
├── Makefile                   # Global compiler and assembler configuration
└── README.md                  # This file
```

---

## 3. Building From Source

### 3.1 Prerequisites
The OS image creation script must be executed on a Linux build host (or a Linux virtual machine / WSL environment) and requires the following utilities:
- `wget`
- `tar`
- `squashfs-tools` (provides `mksquashfs`)
- `grub2` or `grub` tools (provides `grub-mkimage`)
- `go` compiler (v1.20+) - if compiling the Windows installer executable

### 3.2 Compilation Steps
To build the complete project, simply execute the orchestrator commands at the repository root:

```bash
# Build the Alpine root filesystem, download kernel/bootloaders, and compile both installers
make
```

If you only want to build specific targets:
```bash
# Prepare the OS filesystem and download kernel dependencies
make payload

# Compile the Windows Go-based installer executable
make windows-installer

# Build the Linux self-extracting shell installer script
make linux-installer
```

To purge all build directories and reset the workspace:
```bash
make clean
```

---

## 4. USB Installation Guide

Before installing, back up any files on your USB pendrive. Partitioning erases all content on the target device.

### 4.1 Windows Installation
1. Locate `ChimeraStick2Disk.exe` inside the repository directory.
2. Double-click the file to execute it. Windows will prompt for Administrator privileges (required for partitioning).
3. The console will list all connected external USB drives. Enter the drive number corresponding to your pendrive.
4. Verify the drive letters and size, then type `YES` to start the partitioning, formatting, and file-writing routine.
5. Once complete, unplug the USB drive safely.

### 4.2 Linux Installation
1. Open a terminal and navigate to the repository directory.
2. Grant execution privileges to the installer:
   ```bash
   chmod +x ChimeraStick2Disk.sh
   ```
3. Run the installer as root:
   ```bash
   sudo ./ChimeraStick2Disk.sh
   ```
4. Select the target USB device from the terminal menu, confirm by typing `YES`, and the script will construct the server layouts.

---

## 5. Booting and Setup

### 5.1 Booting the Server
1. Plug the installer USB drive into the computer you wish to convert into a server.
2. Turn on the PC and press the Boot Menu key immediately (typically `F12`, `F11`, `F8`, or `ESC` depending on your motherboard).
3. Select your USB drive from the menu (select the UEFI USB option if supported, or legacy USB).
4. The GRUB boot screen will display. Hit enter or wait 3 seconds to boot into the ChimeraStick Personal Server.
5. The system will start. Once booted, you will see a terminal prompt. You can check the local IP of the server using `ip a`.

### 5.2 Accessing the Dashboard & Setup
1. From any device on the same local network (such as your phone or main PC), open a web browser and navigate to the server's local IP address (e.g., `http://192.168.1.100`).
2. **First Run Setup**: The system will automatically detect that no password has been established and show the **Initialize Server** wizard. Create a strong administrator password.
3. **Dashboard**: Once logged in, you will access the sleek, dark glassmorphic control center featuring live performance metrics (CPU, Memory, Disk usage) updated in real-time.
4. **Discord Webhook Setup (Optional)**:
   - Locate the **Discord Notifications** card in the left column.
   - Paste a Discord Channel Webhook URL and click **Save Settings**.
   - Whenever the server starts up or the tunnel goes online, the server will automatically post its new ephemeral public access link, local IP, and system uptime to your Discord channel. This removes the need for a custom domain!
5. **Enabling Remote Access**: Click **Enable Internet Access** under the connection panel. The server will spin up a secure, encrypted Cloudflare tunnel and generate a public HTTPS URL (e.g., `https://your-random-subdomain.trycloudflare.com`).
6. **Accessing Globally**: You can now access your server's web dashboard and file manager from anywhere in the world using this URL over an encrypted HTTPS connection.
7. **File Sharing**: Go to the **Browse Files** section. You can download and delete files, or drag-and-drop new files to upload them directly. All uploaded data is safely written to the physical exFAT partition on the USB drive.

---

## 6. Security and Design Specs

- **Salted Password Protection**: Administration credentials are salted and hashed on-disk using `bcrypt` to prevent brute force cracking.
- **Firewall Guard**: The system blocks incoming external TCP connections by default, routing all remote requests through the outgoing encrypted Cloudflare Tunnel connection on port 80.
- **Wear Prevention**: Root runs purely in RAM to avoid corruptions, meaning you can shut down the host PC simply by pulling the power plug, with no risk to the base operating system.
