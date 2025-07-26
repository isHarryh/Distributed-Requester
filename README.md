Distributed-Requester
==========
Distributed stress testing tool for web servers  
分布式网络服务器压力测试工具

## Introduction

This tool is designed for distributed stress testing of web servers, allowing you to run tests across multiple clients and one server. It supports both offline mode with predefined tasks and distributed mode where clients will fetch tasks from server in advance.

## Get Started

### Requirements

- Python 3.12
- Install dependencies (using poetry or pip)

### Usage

```txt
usage: main.py [-h] [-s] [-c] config_file

Distributed Requester

positional arguments:
  config_file   Configuration file path

options:
  -h, --help    show this help message and exit
  -s, --server  Run in server mode
  -c, --client  Run in client mode
```

If no switch provided, it will run in offline mode with local tasks that defined in the config file.

## Licensing

This project is licensed under the MIT License. See the [License](https://github.com/isHarryh/Distributed-Requester/blob/main/LICENSE) file for more details.
