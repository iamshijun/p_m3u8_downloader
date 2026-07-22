# Migration to Unified Downloader

The `unified_downloader.py` script replaces individual wrapper scripts with intelligent domain detection and configuration management.

## Benefits
- **Single script to maintain** - No more multiple wrapper files
- **Auto-detection of domain requirements** - Automatically applies the right headers and proxy
- **Easy to add new domains** - Just edit the configuration file
- **Full backward compatibility** - All existing functionality continues to work
- **Flexible configuration** - Supports both JSON and TOML formats

## Migration Steps

### 1. Install Optional TOML Support (Recommended)
```bash
pip install toml
```

### 2. Configure Domains
Edit `domain_config.json` or `domain_config.toml` to add your domains:

```json
// domain_config.json
{
  "domains": {
    "your_domain.com": {
      "proxy": "http://your-proxy:8080",
      "headers": {
        "Referer": "https://your_domain.com/"
      }
    }
  }
}
```

### 3. Replace Old Commands

#### Before (Individual Wrappers)
```bash
# missav.ai downloads
./missav_download.sh https://missav.ai/video.m3u8

# 7mmtv.sx downloads
./7mmtv_sp_download.sh https://7mmtv.sx/video.m3u8

# Generic downloads
./start_download.sh https://example.com/video.m3u8
```

#### After (Unified Downloader)
```bash
# Auto-detects domain and applies configuration
python3 unified_downloader.py https://missav.ai/video.m3u8
python3 unified_downloader.py https://7mmtv.sx/video.m3u8 --no-merge
python3 unified_downloader.py https://example.com/video.m3u8
```

### 4. Override Domain Configuration
You can override domain-specific settings:

```bash
# Override proxy for a domain
python3 unified_downloader.py https://missav.ai/video.m3u8 --proxy http://custom-proxy:8080

# Add custom headers
python3 unified_downloader.py https://7mmtv.sx/video.m3u8 -H "Custom-Header: value"
```

## Configuration Format

### JSON Format (Default)
```json
{
  "user_agent": "Mozilla/5.0 (compatible browser)",
  "default_proxy": "http://127.0.0.1:8001",
  "domains": {
    "missav_ai": {
      "proxy": "http://127.0.0.1:8001",
      "headers": {
        "Referer": "https://missav.ai/ja"
      }
    },
    "7mmtv.sx": {
      "proxy": "",
      "headers": {
        "Referer": "https://7mmtv.sx"
      }
    }
  }
}
```

### TOML Format (Recommended if `toml` package is installed)
```toml
user_agent = "Mozilla/5.0 (compatible browser)"
default_proxy = "http://127.0.0.1:8001"

[domains]
missav_ai = { proxy = "http://127.0.0.1:8001", headers = { Referer = "https://missav.ai/ja" } }
"7mmtv.sx" = { proxy = "", headers = { Referer = "https://7mmtv.sx" } }
```

## Domain Matching Rules

1. **Exact match**: `missav_ai` matches `missav.ai`
2. **Partial match**: `7mmtv.sx` matches `sub.7mmtv.sx`
3. **Fallback**: Unknown domains use no configuration

## Tips

- Use `python3 unified_downloader.py --config` to view current configuration
- Use `python3 unified_downloader.py --help` for usage information
- User-provided arguments always override domain configuration
- Empty proxy strings (`""`) disable proxy for that domain