#!/usr/bin/env python3
"""定时监测脚本：运行 strategy_monitor.py，格式化后发送邮件。

用法:
  python3 tools/monitor_alert.py            # 默认：运行监测 + 发邮件
  python3 tools/monitor_alert.py --dry-run  # 只打印邮件内容，不发

环境变量（写入 .env 文件或 export）:
  QQ_SMTP_AUTH_CODE=你的16位授权码
  QQ_SMTP_SENDER=你的QQ号@qq.com
"""

import argparse
import json
import os
import smtplib
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITOR_SCRIPT = os.path.join(PROJECT_DIR, "tools", "strategy_monitor.py")

_CN_TZ = timezone(timedelta(hours=8))


def _load_env():
    """从 .env 和 os.environ 加载 SMTP 配置。"""
    env_path = os.path.join(PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key not in os.environ:
                    os.environ[key] = value

    return {
        "auth_code": os.environ.get("QQ_SMTP_AUTH_CODE", ""),
        "sender": os.environ.get("QQ_SMTP_SENDER", ""),
        "to": os.environ.get("QQ_SMTP_TO", "294873269@qq.com"),
    }


def run_monitor():
    """Run strategy_monitor.py --json and return parsed result."""
    result = subprocess.run(
        [sys.executable, MONITOR_SCRIPT, "--json"],
        capture_output=True, text=True, timeout=120, cwd=PROJECT_DIR,
    )
    if result.returncode != 0:
        return {"error": f"监测脚本执行失败 (exit {result.returncode})", "stderr": result.stderr}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "JSON 解析失败", "stdout": result.stdout}


def _qq_code(code):
    """ETF 代码转腾讯行情代码格式。"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2", "1")):
        return f"sz{code}"
    return f"sh{code}"


def fetch_realtime_prices(codes):
    """通过腾讯行情 API 批量拉取实时价格。

    Returns:
        {code: {"price": str, "change_pct": str, "name": str}, ...}
        拉取失败的 code 不在结果中。
    """
    prices = {}
    for code in codes:
        try:
            url = f"https://qt.gtimg.cn/q={_qq_code(code)}"
            result = subprocess.run(
                ["/usr/bin/curl", "-s", "--noproxy", "*",
                 "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", url],
                capture_output=True, timeout=10)
            raw = result.stdout.decode("gbk", errors="replace")
            start = raw.find('"')
            end = raw.rfind('"')
            if start < 0 or end <= start:
                continue
            fields = raw[start + 1:end].split("~")
            if len(fields) < 40:
                continue
            prices[code] = {
                "price": fields[3],
                "change_pct": fields[32],
                "name": fields[1],
            }
        except Exception:
            pass
    return prices


def _r(value, suffix=""):
    """Format optional numeric values."""
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _fmt_pct(val):
    """Format a float as +x.xx% or -x.xx% with color class."""
    if val is None:
        return ("N/A", "")
    pct = f"{val:+.2f}%"
    if val > 0:
        return (pct, "up")
    elif val < 0:
        return (pct, "down")
    return (pct, "")


def _fmt_num(val, suffix=""):
    """Format optional numeric values, return (str, '')."""
    if val is None:
        return ("N/A", "")
    if isinstance(val, float):
        return (f"{val:.2f}{suffix}", "")
    return (f"{val}{suffix}", "")


def _signal_badge(signal):
    """Return (label, css_class) for signal strength."""
    if signal == "strong":
        return ("强势", "badge-strong")
    elif signal == "medium":
        return ("中性", "badge-medium")
    return ("无信号", "badge-none")


def _yes_no(flag, yes="是", no="否"):
    """Return (text, css_class) for boolean check."""
    if flag:
        return (yes, "yes")
    return (no, "no")


def format_email_body(report, live_prices=None):
    """Render the monitoring report as an HTML email body."""
    now = datetime.now(_CN_TZ).strftime("%Y-%m-%d %H:%M")
    momentum = report.get("momentum")
    grid = report.get("grid", [])
    risk = report.get("risk")
    advice = report.get("advice", {})

    def _price(code, close):
        """Return (price_str, change_str, change_cls)."""
        live = (live_prices or {}).get(code)
        change_str, change_cls = "", ""
        if live:
            try:
                p = f"{float(live['price']):.3f}"
            except (ValueError, TypeError):
                live = None
        if not live:
            p = f"{close:.3f}" if close is not None else "N/A"
        if live and live.get("change_pct"):
            c = float(live["change_pct"])
            change_str = f"{c:+.2f}%"
            change_cls = "up" if c > 0 else "down" if c < 0 else ""
        return (p, change_str, change_cls)

    # ── Momentum rows ──
    momentum_rows = ""
    if momentum:
        for item in momentum.get("items", []):
            code = item.get("code", "-")
            name = item.get("name", "")
            error = item.get("error", "")
            if error:
                momentum_rows += f"""
            <tr><td>{code}</td><td>{name}</td><td colspan="11" class="err">{error}</td></tr>"""
                continue

            price, chg, chg_cls = _price(code, item.get("close"))
            rsrs = _r(item.get("rsrs_score"))
            slope, _ = _fmt_pct(item.get("slope_annual_pct"))
            r2 = f"{item.get('r_squared', 0):.3f}"
            rsi_v = item.get("rsi")
            rsi = f"{rsi_v:.1f}" if rsi_v is not None else "N/A"
            sig_label, sig_cls = _signal_badge(item.get("signal_strength"))
            above_ma = "✅" if item.get("above_ma") else "❌"
            above_ma60 = "✅" if item.get("above_ma60") else "—"
            golden = "🌟" if item.get("golden_cross") else ""
            vol = f"{item.get('vol_20d', 'N/A')}%"
            p5, p5_cls = _fmt_pct(item.get("pct_5d"))
            p20, p20_cls = _fmt_pct(item.get("pct_20d"))
            kdate = item.get("date", "")

            momentum_rows += f"""
            <tr>
              <td class="code">{code}</td><td class="name">{name}{golden}</td>
              <td class="num">{price}</td><td class="num {chg_cls}">{chg}</td>
              <td class="num">{rsrs}</td><td class="num">{slope}</td><td class="num">{r2}</td>
              <td class="num">{rsi}</td>
              <td>{above_ma}</td><td>{above_ma60}</td>
              <td class="num">{vol}</td>
              <td class="num {p5_cls}">{p5}</td><td class="num {p20_cls}">{p20}</td>
              <td><span class="badge {sig_cls}">{sig_label}</span></td>
            </tr>"""

        # 信号分布
        strengths = [i.get("signal_strength") for i in momentum.get("items", [])]
        s_count = f"强 {strengths.count('strong')} · 中 {strengths.count('medium')} · 无 {strengths.count('none')}"
        selected_line = ""
        if momentum.get("selected"):
            sel = momentum["selected"]
            selected_line = f" &nbsp;|&nbsp; 📌 持仓: <b>{sel.get('code')} {sel.get('name', '')}</b>"
        momentum_footer = f"信号分布: {s_count}{selected_line}"
    else:
        momentum_footer = "无数据"

    # ── Grid rows ──
    grid_rows = ""
    if grid:
        for item in grid:
            code = item.get("code", item.get("etf", "-"))
            name = item.get("name", "")
            score = _r(item.get("score"))
            kline_close = item.get("close")
            price, chg, chg_cls = _price(code, kline_close)
            bb = f"{item.get('bb_width')}%" if item.get("bb_width") is not None else "N/A"
            ma20 = _r(item.get("ma20"), "")
            ma60 = _r(item.get("ma60"), "")
            ma_state = item.get("ma_state", "")
            verdict = item.get("verdict", "")

            grid_rows += f"""
            <tr>
              <td class="code">{code}</td><td class="name">{name}</td>
              <td class="num">{price}</td><td class="num {chg_cls}">{chg}</td>
              <td class="num">{score}</td><td class="num">{bb}</td>
              <td class="num">{ma20}</td><td class="num">{ma60}</td>
              <td>{ma_state}</td><td style="font-size:12px">{verdict}</td>
            </tr>"""

        groups = report.get("grid_groups", {})
        stopped = [i.get("code") for i in groups.get("stop", [])]
        grid_footer = f"⛔ 暂停买入: {', '.join(stopped)}" if stopped else ""
    else:
        grid_footer = "无数据"

    # ── Risk ──
    risk_html = ""
    if risk and risk.get("status") == "ok":
        risk_html = f"""
        <tr><td>最大回撤</td><td class="num">{_r(risk.get('max_dd_pct'), '%')}</td></tr>
        <tr><td>Sharpe</td><td class="num">{_r(risk.get('sharpe'))}</td></tr>
        <tr><td>VaR (95%)</td><td class="num">{_r(risk.get('var_95_loss_pct'), '%')}</td></tr>
        <tr><td>IC (10日)</td><td class="num">{_r(risk.get('ic_10d'))}</td></tr>
        <tr><td>IC (20日)</td><td class="num">{_r(risk.get('ic_20d'))}</td></tr>"""

    error_banner = ""
    if report.get("error"):
        error_banner = f'<div class="error-banner">⚠️ {report["error"]}</div>'

    # ── Compose HTML ──
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  .up {{ color:#38a169; font-weight:600 }}
  .down {{ color:#e53e3e; font-weight:600 }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600 }}
  .badge-strong {{ background:#c6f6d5; color:#22543d }}
  .badge-medium {{ background:#fefcbf; color:#744210 }}
  .badge-none {{ background:#edf2f7; color:#4a5568 }}
  .code {{ font-family:SF Mono,Monaco,Consolas,monospace; font-size:12px }}
  .name {{ font-weight:500 }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums }}
  .err {{ color:#e53e3e; font-size:12px }}
  .error-banner {{ background:#fff5f5; color:#c53030; padding:12px 28px; font-size:13px; border-bottom:2px solid #fc8181 }}
</style>
</head>
<body style="margin:0;padding:0;background:#f5f6fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6fa;padding:16px 0">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)">

  <!-- Header -->
  <tr><td style="background:#1a1a2e;padding:24px 28px">
    <div style="font-size:20px;font-weight:700;color:#fff;margin:0">📊 双策略监测报告</div>
    <div style="font-size:13px;color:#8892b0;margin-top:4px">{now}</div>
  </td></tr>

  {error_banner}

  <!-- == Momentum == -->
  <tr><td style="padding:20px 28px 8px">
    <div style="font-size:16px;font-weight:700;color:#1a1a2e;border-left:4px solid #4a6cf7;padding-left:10px">动量轮动 · RSRS 信号</div>
  </td></tr>
  <tr><td style="padding:0 28px">
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
      <thead>
        <tr style="background:#f0f2ff;color:#4a5568">
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:left">代码</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:left">名称</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">现价</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">涨跌</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">RSRS</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">斜率</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">R²</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">RSI</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0">MA20</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0">MA60</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">波动</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">5日</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">20日</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0">信号</th>
        </tr>
      </thead>
      <tbody>{momentum_rows}
      </tbody>
    </table>
    <div style="font-size:12px;color:#718096;padding:10px 0 16px">{momentum_footer}</div>
  </td></tr>

  <!-- == Grid == -->
  <tr><td style="padding:8px 28px">
    <div style="font-size:16px;font-weight:700;color:#1a1a2e;border-left:4px solid #f6ad55;padding-left:10px">网格策略 · 持仓趋势</div>
  </td></tr>
  <tr><td style="padding:0 28px">
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
      <thead>
        <tr style="background:#fffaf0;color:#4a5568">
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:left">代码</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:left">名称</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">现价</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">涨跌</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">评分</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">BB宽</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">MA20</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0;text-align:right">MA60</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0">状态</th>
          <th style="padding:8px 6px;border-bottom:2px solid #e2e8f0">判断</th>
        </tr>
      </thead>
      <tbody>{grid_rows}
      </tbody>
    </table>
    <div style="font-size:12px;color:#718096;padding:10px 0 16px">{grid_footer}</div>
  </td></tr>

  <!-- == Risk + Advice == -->
  <tr><td style="padding:8px 28px 20px">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="48%" style="vertical-align:top;padding-right:12px">
          <div style="font-size:16px;font-weight:700;color:#1a1a2e;border-left:4px solid #e53e3e;padding-left:10px;margin-bottom:8px">风险审计</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
            <tbody>{risk_html or '<tr><td style="color:#a0aec0">状态异常或无数据</td></tr>'}
            </tbody>
          </table>
        </td>
        <td width="4%"></td>
        <td width="48%" style="vertical-align:top;padding-left:12px">
          <div style="font-size:16px;font-weight:700;color:#1a1a2e;border-left:4px solid #48bb78;padding-left:10px;margin-bottom:8px">操作建议</div>
          <table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
            <tr><td style="padding:4px 0;color:#4a5568">🔄 动量</td><td>{advice.get('momentum_action') or '无正式动作'}</td></tr>
            <tr><td style="padding:4px 0;color:#4a5568">📐 网格</td><td>{advice.get('grid_action') or '无正式动作'}</td></tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f7f8fc;padding:12px 28px;text-align:center;font-size:11px;color:#a0aec0">
    由 tools/monitor_alert.py 自动生成 · 仅供参考
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""
    return html


def send_email(smtp_config, body, max_retries=3, retry_delay=5):
    """发送监测报告邮件，失败自动重试。

    重试策略：DNS 解析失败、连接超时等瞬时错误等待 retry_delay 秒后重试，
    最多重试 max_retries 次。认证失败不重试。
    """
    sender = smtp_config["sender"]
    to = smtp_config["to"]
    auth_code = smtp_config["auth_code"]

    if not sender or not auth_code:
        raise RuntimeError("SMTP 未配置，请设置 QQ_SMTP_AUTH_CODE 和 QQ_SMTP_SENDER 环境变量")

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"策略监测报告 {datetime.now(_CN_TZ).strftime('%Y-%m-%d %H:%M')}"

    msg.attach(MIMEText(body, "html", "utf-8"))

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=15)
            try:
                server.login(sender, auth_code)
                server.sendmail(sender, [to], msg.as_string())
            finally:
                server.quit()
            return  # 发送成功
        except smtplib.SMTPAuthenticationError:
            raise  # 认证失败不重试
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                print(f"  邮件发送失败 (第{attempt}/{max_retries}次): {exc}，{retry_delay}秒后重试...")
                time.sleep(retry_delay)
            else:
                raise RuntimeError(f"邮件发送失败（已重试{max_retries}次）: {last_error}")


def main():
    parser = argparse.ArgumentParser(description="双策略监测 + 邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="只打印邮件内容，不发")
    args = parser.parse_args()

    smtp_config = _load_env()

    print(f"[{datetime.now(_CN_TZ).strftime('%Y-%m-%d %H:%M:%S')}] 运行监测...")
    report = run_monitor()

    # 收集所有 ETF 代码，拉取实时行情
    etf_codes = set()
    for item in report.get("momentum", {}).get("items", []):
        code = item.get("code", "")
        if code and not item.get("error"):
            etf_codes.add(code)
    for item in report.get("grid", []):
        code = item.get("code", item.get("etf", ""))
        if code and item.get("status") != "unknown":
            etf_codes.add(code)
    live_prices = fetch_realtime_prices(etf_codes) if etf_codes else {}

    if "error" in report and "momentum" not in report:
        print(f"  错误: {report['error']}")
        body = f"监测脚本异常\n\n{json.dumps(report, ensure_ascii=False, indent=2)}"
    else:
        body = format_email_body(report, live_prices)
        print(f"  完成 (实时行情: {len(live_prices)}/{len(etf_codes)})")

    print(body)

    if args.dry_run:
        print("\n--- dry-run 模式，未发送邮件 ---")
        return

    if not smtp_config["auth_code"]:
        print("\n未配置 QQ_SMTP_AUTH_CODE，跳过发送邮件。")
        print("在 .env 中设置 QQ_SMTP_AUTH_CODE=你的授权码 后重试。")
        sys.exit(1)

    try:
        send_email(smtp_config, body)
        print("  邮件已发送")
    except Exception as exc:
        print(f"  邮件发送失败: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
