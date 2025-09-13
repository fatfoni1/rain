import asyncio
import random
import traceback
from typing import Any, Dict, List, Optional, Tuple
from playwright.async_api import async_playwright

from asf_core import (
    load_accounts,
    load_seed_phrases,
    save_accounts,
    update_account_token,
)
from asf_core import (
    send_telegram as core_send_telegram,
)

# ====================== Telegram (verbose) ======================
async def send_telegram(text: str, context=None):
    try:
        if text:
            await core_send_telegram(str(text), context)
    except Exception:
        pass

# ====================== Timing / Retry ======================
def _rand_delay(lo: float = 0.5, hi: float = 1.0) -> float:
    try:
        lo = float(lo)
        hi = float(hi)
        if hi < lo:
            hi = lo
    except Exception:
        lo, hi = 0.5, 1.0
    return random.uniform(lo, hi)

async def _sleep(lo: float = 0.5, hi: float = 1.0) -> None:
    await asyncio.sleep(_rand_delay(lo, hi))

async def wait_and_click(ctx, selectors: List[str], step: str, max_retry: int = 100) -> bool:
    for _ in range(max_retry):
        for sel in selectors:
            try:
                el = await ctx.query_selector(sel)
                if not el:
                    continue
                
                vis = await el.is_visible() if hasattr(el, "is_visible") else True
                ena = await el.is_enabled() if hasattr(el, "is_enabled") else True
                if not (vis and ena):
                    continue
                
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                
                await asyncio.sleep(0.1)
                
                try:
                    await el.click()
                except Exception:
                    try:
                        await ctx.locator(sel).first.click(force=True, timeout=1500)
                    except Exception:
                        try:
                            await ctx.evaluate(
                                "s => {const n=document.querySelector(s); if(!n) return false; n.scrollIntoView({block:'center'}); n.click(); return true;}",
                                sel,
                            )
                        except Exception:
                            await _sleep()
                            continue
                
                await send_telegram(f"✅ Klik {step}")
                await _sleep()
                return True
            except Exception:
                continue
        await _sleep()
    
    await send_telegram(f"❌ Gagal klik {step} setelah {max_retry}x")
    return False

async def wait_and_paste(ctx, selector: str, value: str, step: str, max_retry: int = 100) -> bool:
    for _ in range(max_retry):
        try:
            el = await ctx.query_selector(selector)
            if el:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                
                await asyncio.sleep(0.1)
                
                try:
                    await el.fill(value)  # paste langsung
                except Exception:
                    await _sleep()
                    continue
                
                await send_telegram(f"✅ Paste {step}")
                await _sleep()
                return True
        except Exception:
            pass
        await _sleep()
    
    await send_telegram(f"❌ Gagal paste {step} setelah {max_retry}x")
    return False

async def wait_visible_any(ctx, selectors: List[str], max_retry: int = 100) -> bool:
    for _ in range(max_retry):
        for sel in selectors:
            try:
                el = await ctx.query_selector(sel)
                if el:
                    vis = await el.is_visible() if hasattr(el, "is_visible") else True
                    if vis:
                        return True
            except Exception:
                pass
        await _sleep(0.5, 0.9)
    return False

# ====================== Seed dummy (onboard) ======================
async def paste_seed_dummy(page, seed_phrase: str) -> bool:
    # 1) tombol "Paste" bawaan
    try:
        try:
            await page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        
        try:
            await page.evaluate(
                """async (t)=>{try{await navigator.clipboard.writeText(t);return true;}catch(e){return false;}}""",
                seed_phrase,
            )
        except Exception:
            pass
        
        ok_btn = await wait_and_click(
            page, ["button:has-text('Paste')", "text=Paste"], "Paste Seed Dummy"
        )
        
        if ok_btn:
            for _ in range(40):
                v1 = await page.get_attribute('[data-testid="input-recovery-phrase-1"]', "value")
                v12 = await page.get_attribute('[data-testid="input-recovery-phrase-12"]', "value")
                if (v1 or "").strip() and (v12 or "").strip():
                    await send_telegram("✅ Seed Dummy via tombol Paste")
                    return True
                await _sleep(0.2, 0.4)
    except Exception:
        pass
    
    # 2) fallback isi 12 kolom
    try:
        words = [w.strip() for w in seed_phrase.split() if w.strip()]
        if len(words) != 12:
            await send_telegram("❌ Seed Dummy bukan 12 kata (fallback batal)")
            return False
        
        for i, w in enumerate(words, start=1):
            sel = f'[data-testid="input-recovery-phrase-{i}"]'
            if not await wait_and_paste(page, sel, w, f"Seed Dummy #{i}"):
                return False
        
        await send_telegram("✅ Seed Dummy via fallback 12 kolom")
        return True
    except Exception:
        await send_telegram(f"❌ Gagal fallback Seed Dummy\n{traceback.format_exc()}")
        return False

# ====================== Wallet pickers ======================
async def _select_wallet_by_name(page, name: str) -> bool:
    target = (name or "").strip().lower()
    for _ in range(100):
        try:
            items = await page.query_selector_all('[data-testid^="li-wallets-"]')
            for it in items:
                t = await it.query_selector('[data-testid="list-item-m-title"]')
                if not t:
                    continue
                txt = (await t.inner_text()).strip().lower()
                if txt == target:
                    try:
                        await it.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await it.click()
                    return True
        except Exception:
            pass
        await _sleep()
    return False

async def _click_imported_wallet(page, name: str) -> bool:
    """Klik wallet pada SECTION 'Imported'."""
    target = (name or "").strip().lower()
    
    await wait_visible_any(
        page,
        [
            '[data-testid="section-header_account_imported"]',
            "text=Imported",
            '[data-testid^="li-wallets-"]',
        ],
        max_retry=100,
    )
    
    # 1) strict in-section
    try:
        handle = await page.evaluate_handle(
            """(targetName) => {
                const sec = document.querySelector('[data-testid="section-header_account_imported"]');
                if (!sec) return null;
                const btns = sec.querySelectorAll('button[data-testid^="li-wallets-"]');
                for (const b of btns) {
                    const t = b.querySelector('[data-testid="list-item-m-title"]');
                    const txt = (t && t.textContent || '').trim().toLowerCase();
                    if (txt === targetName) return b;
                }
                return null;
            }""",
            target,
        )
        
        if handle:
            el = handle.as_element()
            if el:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await el.click()
                except Exception:
                    await page.evaluate("(e)=>{e.click();}", el)
                await send_telegram(f"✅ Pilih wallet Imported: {name}")
                return True
    except Exception:
        pass
    
    # 2) first-in-section
    try:
        first = await page.evaluate_handle(
            """() => {
                const sec = document.querySelector('[data-testid="section-header_account_imported"]');
                if (!sec) return null;
                return sec.querySelector('button[data-testid^="li-wallets-"]') || null;
            }"""
        )
        
        if first:
            el = first.as_element()
            if el:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await el.click()
                except Exception:
                    await page.evaluate("(e)=>{e.click();}", el)
                await send_telegram("⚠️ Fallback: klik wallet Imported pertama")
                return True
    except Exception:
        pass
    
    # 3) global
    if await _select_wallet_by_name(page, name):
        await send_telegram(f"⚠️ Fallback global: pilih wallet {name}")
        return True
    
    await send_telegram(f"❌ Gagal pilih wallet Imported: {name}")
    return False

# ====================== Solflare surfaces ======================
def _solflare_frames(page) -> List[Any]:
    try:
        return [
            fr
            for fr in page.frames
            if any(k in (fr.url or "").lower() for k in ["solflare", "connect.solflare.com"])
        ]
    except Exception:
        return []

def _solflare_pages(page) -> List[Any]:
    try:
        pages = []
        for p in page.context.pages:
            u = (p.url or "").lower()
            if any(
                k in u for k in ["solflare.com/provider", "connect.solflare.com", "solflare.com"]
            ):
                pages.append(p)
        return pages
    except Exception:
        return []

async def _click_on_solflare_only(page, selectors: List[str], step: str, tries: int = 100) -> bool:
    """Klik hanya di surface Solflare (popup/iframe)."""
    for _ in range(tries):
        # popup pages
        try:
            for sp in _solflare_pages(page):
                for sel in selectors:
                    try:
                        el = await sp.query_selector(sel)
                        if el:
                            try:
                                await el.scroll_into_view_if_needed()
                            except Exception:
                                pass
                            await el.click()
                            await send_telegram(f"✅ {step} (popup)")
                            await _sleep()
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        
        # frames
        try:
            for fr in _solflare_frames(page):
                for sel in selectors:
                    try:
                        el = await fr.query_selector(sel)
                        if el:
                            try:
                                await el.scroll_into_view_if_needed()
                            except Exception:
                                pass
                            await el.click()
                            await send_telegram(f"✅ {step} (frame)")
                            await _sleep()
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        
        await _sleep()
    
    await send_telegram(f"❌ Gagal {step} (Solflare) setelah {tries}x")
    return False

# ====================== JWT util + token polling ======================
def _is_jwt(s: Optional[str]) -> bool:
    try:
        return (
            bool(s)
            and isinstance(s, str)
            and s.startswith("eyJ")
            and s.count(".") == 2
            and len(s) > 50
        )
    except Exception:
        return False

async def _try_get_token(page, attempts: int = 40) -> Optional[str]:
    """Polling token dari localStorage/sessionStorage tanpa reload."""
    for _ in range(attempts):
        try:
            tok = await page.evaluate(
                "window.localStorage.getItem('token') || window.sessionStorage.getItem('token')"
            )
        except Exception:
            tok = None
        
        if _is_jwt(tok):
            return tok
        
        await _sleep(0.6, 1.0)
    
    return None

# ====================== Login via Private Key ======================
async def login_with_private_key(browser, account_name: str, private_key: str) -> Optional[str]:
    """Login dengan private key dan return token JWT"""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    in_solflare_phase = False
    
    try:
        # 1) Onboard
        await send_telegram(f"🌐 {account_name} | Buka Solflare Onboard")
        await page.goto("https://solflare.com/onboard/access", timeout=60000)
        await _sleep()
        
        seed_dummy = "net ill reflect stomach abuse satoshi pilot pact unusual leg canvas auction"
        if not await paste_seed_dummy(page, seed_dummy):
            await send_telegram(f"❌ {account_name} | Gagal isi seed dummy")
            return None
        
        if not await wait_and_click(page, ['[data-testid="btn-continue"]'], "Continue (Seed)"):
            return None
        
        if not await wait_and_paste(
            page, '[data-testid="input-new-password"]', "fatoni11", "Password Baru"
        ):
            return None
        
        if not await wait_and_paste(
            page, '[data-testid="input-repeat-password"]', "fatoni11", "Ulangi Password"
        ):
            return None
        
        if not await wait_and_click(page, ['[data-testid="btn-continue"]'], "Continue (Password)"):
            return None
        
        if not await wait_and_click(page, ['[data-testid="btn-quick-setup"]'], "Quick setup"):
            return None
        
        if not await wait_and_click(page, ['[data-testid="btn-explore"]'], "I agree, let's go"):
            return None
        
        # 2) Import PK
        await send_telegram(f"🔑 {account_name} | Import Private Key")
        await page.goto(
            "https://solflare.com/wallet-management/options/import/private-key", timeout=60000
        )
        await _sleep()
        
        if not await wait_and_paste(
            page, '[data-testid="input-name"]', account_name, "Wallet Name"
        ):
            return None
        
        if not await wait_and_paste(
            page, '[data-testid="input-private-key"]', private_key, "Private Key"
        ):
            return None
        
        if not await wait_and_click(page, ['[data-testid="btn-import"]'], "Import"):
            return None
        
        # tunggu /wallet-management & pilih wallet Imported
        try:
            await page.wait_for_url("**/wallet-management**", timeout=20000)
        except Exception:
            pass
        
        if not await _click_imported_wallet(page, account_name):
            return None
        
        # 3) flip.gg connect
        await send_telegram(f"🌐 {account_name} | Buka flip.gg")
        await page.goto("https://flip.gg/", timeout=60000)
        await _sleep(0.7, 1.0)
        
        # Connect wallet button
        if not await wait_and_click(page, [".tss-47m19k-connect"], "Connect Wallet (toolbar)"):
            return None
        
        if not await wait_and_click(
            page,
            ["button:has-text('Solflare')", ".wallet-adapter-button:has-text('Solflare')"],
            "Pilih Solflare",
        ):
            return None
        
        # Use Web Wallet
        if not await _click_on_solflare_only(
            page,
            [
                "#connect-web-button",
                "a.css-dxb75a",
                "a:has-text('Use Web Wallet')",
                "text=Use Web Wallet",
            ],
            "Use Web Wallet",
            tries=100,
        ):
            return None
        
        in_solflare_phase = True
        
        # CONNECT (Solflare only)
        if not await _click_on_solflare_only(
            page,
            ["button[data-testid='btn-connect']", "button:has-text('Connect')", "text=Connect"],
            "Connect (Solflare)",
            tries=100,
        ):
            return None
        
        # APPROVE (Solflare only)
        if not await _click_on_solflare_only(
            page,
            ["button[data-testid='btn-approve']", "button:has-text('Approve')", "text=Approve"],
            "Approve",
            tries=100,
        ):
            return None
        
        in_solflare_phase = False
        
        # Tunggu token
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        
        token = await _try_get_token(page, attempts=40)
        
        if not _is_jwt(token):
            # Handle "Wallet not connected!" error
            try:
                for _ in range(3):
                    err = await page.query_selector("text='Wallet not connected!'")
                    if err:
                        await send_telegram(
                            f"⚠️ {account_name} | Wallet not connected! Reload & retry connect"
                        )
                        await page.reload()
                        await _sleep(0.7, 1.0)
                        
                        # Retry connect
                        if not await wait_and_click(
                            page, [".tss-47m19k-connect"], "Connect Wallet (retry)"
                        ):
                            break
                        
                        if not await wait_and_click(
                            page, ["button:has-text('Solflare')"], "Pilih Solflare (retry)"
                        ):
                            break
                        
                        # Retry Solflare flow
                        if not await _click_on_solflare_only(
                            page,
                            [
                                "#connect-web-button",
                                "a.css-dxb75a",
                                "a:has-text('Use Web Wallet')",
                                "text=Use Web Wallet",
                            ],
                            "Use Web Wallet (retry)",
                            tries=100,
                        ):
                            break
                        
                        if not await _click_on_solflare_only(
                            page,
                            [
                                "button[data-testid='btn-connect']",
                                "button:has-text('Connect')",
                                "text=Connect",
                            ],
                            "Connect (retry, Solflare)",
                            tries=100,
                        ):
                            break
                        
                        if not await _click_on_solflare_only(
                            page,
                            [
                                "button[data-testid='btn-approve']",
                                "button:has-text('Approve')",
                                "text=Approve",
                            ],
                            "Approve (retry)",
                            tries=100,
                        ):
                            break
                        
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        
                        token = await _try_get_token(page, attempts=20)
                        if _is_jwt(token):
                            break
                    else:
                        break
            except Exception:
                pass
        
        if not _is_jwt(token):
            await send_telegram(f"ℹ️ {account_name} | Token belum muncul, refresh sekali")
            await page.reload()
            await _sleep(0.7, 1.0)
            token = await _try_get_token(page, attempts=40)
        
        if _is_jwt(token):
            await send_telegram(f"✅ {account_name} | Token berhasil diperbarui")
            return token
        
        await send_telegram(f"❌ {account_name} | Token tidak ditemukan")
        return None
    
    except Exception as e:
        await send_telegram(f"❌ {account_name} | Error: {e}\n{traceback.format_exc()}")
        return None
    
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

# ====================== Back-Compat APIs ======================
async def refresh_invalid_tokens(
    headless: bool = True,
    context: Optional[Any] = None,
    log_func: Optional[Any] = None,
    stop_event: Optional[Any] = None,
    invalid_names: Optional[List[str]] = None,
    max_concurrency: int = 1,  # Reduced for stability
):
    """Refresh token untuk akun yang invalid"""
    akun_list = load_accounts() or []
    akun_map = {(a.get("name") or "").strip().lower(): a for a in akun_list}
    pk_map = load_seed_phrases() or {}
    pk_norm: Dict[str, str] = {k.strip(): v.strip() for k, v in pk_map.items() if k and v}
    
    if invalid_names:
        s = set([n.strip().lower() for n in invalid_names if n.strip()])
        target_names = [n for n in pk_norm.keys() if n.strip().lower() in s]
    else:
        target_names = list(pk_norm.keys())
    
    if not target_names:
        await send_telegram("ℹ️ Tidak ada akun yang perlu diproses.", context)
        return
    
    await send_telegram(f"▶️ Mulai refresh (Private Key): {len(target_names)} akun", context)
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        
        updated = 0
        failed = 0
        
        for name in target_names:
            pk = pk_norm.get(name, "")
            if not pk:
                await send_telegram(f"❌ {name} | Private key tidak ditemukan", context)
                failed += 1
                continue
            
            token = await login_with_private_key(browser, name, pk)
            if token and _is_jwt(token):
                # Update token in akun.txt
                if update_account_token(name, token):
                    updated += 1
                    await send_telegram(f"✅ {name} | Token berhasil diperbarui", context)
                else:
                    failed += 1
                    await send_telegram(f"❌ {name} | Gagal update token ke file", context)
            else:
                failed += 1
                await send_telegram(f"❌ {name} | Gagal refresh token", context)
        
        try:
            await browser.close()
        except Exception:
            pass
    
    await send_telegram(
        f"🎉 Refresh selesai!\n"
        f"✅ Berhasil: {updated}\n"
        f"❌ Gagal: {failed}\n"
        f"📊 Total: {len(target_names)}",
        context,
    )

async def add_accounts_via_seed(
    headless: bool = True,
    context: Optional[Any] = None,
    log_func: Optional[Any] = None,
    stop_event: Optional[Any] = None,
    max_concurrency: int = 1,
) -> Dict[str, Any]:
    """Tambah akun baru via private key"""
    akun_list = load_accounts() or []
    existing = {(a.get("name") or "").strip().lower() for a in akun_list}
    pk_map = load_seed_phrases() or {}
    
    candidates: List[Tuple[str, str]] = []
    if isinstance(pk_map, dict):
        for name, pk in pk_map.items():
            n = (name or "").strip()
            p = (pk or "").strip()
            if not n or not p:
                continue
            if n.lower() in existing:
                continue
            candidates.append((n, p))
    
    await send_telegram(f"📦 Kandidat akun baru (via Private Key): {len(candidates)}", context)
    
    if not candidates:
        return {"candidates": 0, "added": 0, "failed": 0}
    
    added = 0
    failed = 0
    
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        
        for name, pk in candidates:
            if log_func:
                try:
                    log_func(f"➕ Add via PK: {name}")
                except Exception:
                    pass
            
            await send_telegram(f"🔄 {name} | Login via Private Key (add)", context)
            
            token = await login_with_private_key(browser, name, pk)
            if token and _is_jwt(token):
                if update_account_token(name, token):
                    added += 1
                    await send_telegram(f"✅ Ditambahkan: {name}", context)
                else:
                    failed += 1
                    await send_telegram(f"❌ Gagal simpan ke file: {name}", context)
            else:
                failed += 1
                await send_telegram(f"❌ Gagal ambil token: {name}", context)
        
        try:
            await browser.close()
        except Exception:
            pass
    
    await send_telegram(
        "🎉 Tambah akun via Private Key selesai!\n"
        f"✅ Berhasil: {added}\n"
        f"❌ Gagal: {failed}\n"
        f"📊 Total kandidat: {len(candidates)}",
        context,
    )
    
    return {"candidates": len(candidates), "added": added, "failed": failed}

# ====================== Quick test ======================
async def _test():
    await refresh_invalid_tokens(headless=False)

if __name__ == "__main__":
    asyncio.run(_test())