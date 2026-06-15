# Contributing to ChimeraStick

Thank you for your interest in contributing to ChimeraStick! As an open-source project, we welcome contributions of all forms, including bug reports, documentation updates, feature requests, and pull requests.

This document guides you through setting up a development environment, understanding our system architecture, running checks locally, and submitting code modifications.

---

## 1. Project Directory Layout

Before modifying the code, review our repository structure:

* `/src/chimera-server/` - Python Flask dashboard backend and static glassmorphic web interface resources.
* `/src/build/` - Shell scripts and configurations used to build the immutable Alpine root filesystem.
* `/src/installer/` - Go (Windows) and Bash (Linux) installers designed to partition and flash target USB drives.
* `/payload/` - Payload stub containing default bootloader configurations, modified at install-time.
* `/tests/` - Unit and integration tests written in Python.
* `/.github/workflows/` - GitHub Actions CI/CD configuration files.

---

## 2. Local Development Setup

To test changes to the web dashboard locally without booting the live USB system:

### 2.1 Python Environment Configuration
1. Initialize a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```
2. Install dependencies:
   ```bash
   pip install -r src/chimera-server/requirements.txt
   pip install flake8 black
   ```
3. Run the development server:
   ```bash
   python src/chimera-server/app.py
   ```
4. Access the local dashboard at `http://127.0.0.1:8080`.
   * **Note**: When running on Windows or non-mounted Linux hosts, the application automatically redirects configuration files to `./data/` instead of `/media/data/` to avoid permission errors. Resource telemetry will show safe `0%` fallbacks due to missing `/proc` interfaces.

---

## 3. Running Code Quality Verification

To ensure your code meets our production quality standards before pushing, run the following commands:

### 3.1 Python Linting & Formatting
We enforce `black` code styling and `flake8` syntax checking.
```bash
# Code formatter check
black --check src/chimera-server/ tests/

# Syntax verification
flake8 src/chimera-server/ --count --select=E9,F63,F7,F82 --show-source --statistics
```

### 3.2 Automated Testing
Run the integration and unit tests before committing:
```bash
python -m unittest tests/test_app.py
```

### 3.3 Shell Script Verification
Ensure bash scripts adhere to POSIX safety rules using ShellCheck:
```bash
shellcheck src/build/build_rootfs.sh
shellcheck src/installer/linux/ChimeraStick2Disk.sh
```

---

## 4. RootFS & Bootloader Builds (Linux Only)

Compiling the production bootable SquashFS root filesystem requires root chroot capabilities.

1. Ensure the required tools are installed:
   ```bash
   sudo apt update && sudo apt install -y squashfs-tools grub2-common wget tar
   ```
2. Build the OS target:
   ```bash
   # Downloads Alpine packages, sets up Nginx/Flask OpenRC services, and generates rootfs.squashfs
   sudo make payload
   ```

### UEFI Secure Boot Signed Bootloader Configuration:
During the `make payload` execution, the Makefile downloads official, pre-signed binaries from Debian pools (`shim-signed` and `grub-efi-amd64-signed`). The signed shim (`bootx64.efi`) and signed GRUB (`grubx64.efi`) are placed under `payload/efi/EFI/BOOT/` on Partition 1 alongside a redirecting `grub.cfg` configuration. This redirects GRUB to search for Partition 2 by its label (`BOOT`) to run the main kernel loader, providing out-of-the-box Secure Boot compatibility without manual certificate enrolling.

---

## 5. Submitting Pull Requests

### 5.1 Branching Strategy
* Always branch your modifications from the `main` branch: `git checkout -b feature/your-feature-name`.
* Avoid submitting giant PRs; keep changes targeted and reviewable.

### 5.2 Pull Request Checklist
Before requesting maintainer reviews, verify that:
1. **Tests Pass**: Integration tests run successfully locally.
2. **Linting Clear**: No formatting warnings are raised by `black` or `flake8`.
3. **No Default Credentials**: Verify that no keys, configurations, or credentials have been committed.
4. **Documentation**: Any new config parameters or CLI switches are documented in `README.md`.
5. **No Traversal Paths**: Verify any new directory integrations use `os.path.basename` to prevent traversal vulnerabilities.

Once submitted, our **GitHub Actions CI Pipeline** will verify python tests, Go compilation, and shell syntax rules automatically.
