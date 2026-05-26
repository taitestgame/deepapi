# 🚀 DeepSeek Web-to-API Bridge (Python)

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey.svg)]()

Dự án cầu nối trung gian chuyển đổi giao diện web của **DeepSeek Chat** thành chuẩn API tương thích hoàn toàn với **OpenAI (API Endpoint)**. Dự án được tối ưu hóa đặc biệt dành cho các AI Agent (Cline, Roo Code, Qwen Companion...) chạy ổn định trên máy cá nhân mà không lo bị chặn bởi Cloudflare.

---

## ✨ Tính Năng Nổi Bật

- 🥷 **Bypass Cloudflare & Bot-Detection**: Sử dụng `cloakbrowser` (Playwright) mô phỏng vân tay trình duyệt thật (Chrome/Windows), vượt qua mọi chốt chặn bot.
- 🔄 **Xoay Vòng Nhiều Tài Khoản (Account Rotation)**: Tự động xoay vòng nhiều tài khoản cấu hình qua file `.env` bằng thuật toán Round-robin để tránh dính giới hạn cuộc gọi (Rate Limit).
- 🛠️ **Tự Động Đăng Nhập Lại (Auto-Recovery)**: Phát hiện và tự động đăng nhập lại khi token/phiên làm việc của DeepSeek hết hạn mà không làm gián đoạn Agent.
- ⚙️ **Cấu Hình Tiện Lợi**: Quản lý thông qua file cấu hình môi trường `.env` an toàn và trực quan.

---

## 🛠️ Hướng Dẫn Cài Đặt Chi Tiết

### Bước 1: Cài đặt thư viện Python
Mở Terminal tại thư mục dự án và cài đặt các thư viện cần thiết:
```bash
pip install -r requirements.txt
```

### Bước 2: Tải xuống Trình duyệt CloakBrowser (Bắt buộc)
Do thư viện cần một trình duyệt Chromium tùy chỉnh vân tay đặc biệt để vượt qua Cloudflare, bạn cần tải gói zip này:
- 🔗 **Link tải trực tiếp (cho Windows 64-bit)**: [cloakbrowser-windows-x64.zip](https://github.com/CloakHQ/cloakbrowser/releases/download/chromium-v146.0.7680.177.5/cloakbrowser-windows-x64.zip)

#### Cách cài đặt thủ công:
Sau khi tải xuống, bạn hãy giải nén toàn bộ nội dung trong file zip vào đường dẫn thư mục sau trên máy tính của bạn:
- **Windows**: `C:\Users\<Tên_User_Của_Bạn>\.cloakbrowser\chromium-146.0.7680.177.5\`
*(Đảm bảo file `chrome.exe` nằm trực tiếp ngay trong thư mục `chromium-146.0.7680.177.5`)*

---

### Bước 3: Cấu hình File `.env`
1. Copy file `.env.example` thành file `.env`
2. Mở file `.env` lên và điền cấu hình tài khoản DeepSeek của bạn:

```ini
# Đăng nhập 1 tài khoản mặc định
DEEPSEEK_EMAIL=your_email@gmail.com
DEEPSEEK_PASSWORD=your_password_here

# HOẶC xoay vòng nhiều tài khoản cùng lúc (ngăn cách bằng dấu phẩy)
# DEEPSEEK_ACCOUNTS=email1@gmail.com:pass1,email2@gmail.com:pass2

# Cấu hình API kết nối
API_KEY=sk-my-secret-key-1
PORT=5001
HOST=0.0.0.0
```

---

## 🚀 Cách Sử Dụng

### 1. Khởi chạy Server trung gian:
Chạy lệnh sau tại thư mục dự án để kích hoạt Flask API Server:
```bash
python server.py
```
Khi hiển thị thông báo `[auth] Login OK` và Server chạy ở cổng `5001` là bạn đã cấu hình thành công!

### 2. Cấu hình trên các AI Agent (Cline, Roo Code...):
Truyền các tham số sau vào phần cấu hình của Agent để liên kết:
- **Provider (Nhà cung cấp)**: Chọn `OpenAI Compatible` (hoặc `Custom Provider`)
- **Base URL**: `http://localhost:5001/v1`
- **API Key**: `sk-my-secret-key-1` *(hoặc key bạn tự đặt trong file `.env`)*
- **Model ID**: `deepseek-v4-flash` *(hoặc mô hình nâng cao `deepseek-v4-pro`)*

---

## ⚠️ Miễn Trừ Trách Nhiệm

Dự án này được viết ra nhằm mục đích học tập, nghiên cứu cá nhân và tìm hiểu cơ chế hoạt động của API. Tác giả không chịu trách nhiệm cho bất kỳ vấn đề nào liên quan đến tài khoản của bạn trên nền tảng gốc.
