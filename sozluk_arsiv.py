#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sözlük Arşiv Okuyucu
-----------------------
Eski Ekşi Sözlük (sozluk.sourtimes.org/show.asp?t=...) başlıklarını
archive.org Wayback Machine'de arar, bulunan arşiv tarihlerini listeler
ve seçilen kopyayı okunabilir metin olarak gösterir.

Yalnızca Python standart kütüphanesini kullanır (Python 3.8+).
Çalıştırmak için:  python3 sozluk_arsiv.py
"""

import json
import queue
import re
import socket
import ssl
import threading
import webbrowser
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote_to_bytes, urlencode, urlsplit
from urllib.request import Request, urlopen

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

APP_TITLE = "Sözlük Arşiv Okuyucu"
HOST = "sozluk.sourtimes.org"
CDX_API = "https://web.archive.org/cdx/search/cdx"
UA = "SozlukArsivOkuyucu/1.0 (kisisel arsiv okuma)"


# ----------------------------------------------------------------- yardımcılar

def tr_lower(s):
    """Türkçe kurallarına uygun küçük harf (I -> ı, İ -> i)."""
    return s.replace("I", "ı").replace("İ", "i").lower()


def norm_title(s):
    return " ".join(tr_lower(s or "").split())


def _ssl_context():
    try:
        import certifi  # varsa daha güncel sertifika deposu
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def http_get(url, timeout=90):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read(), r.headers.get_content_charset()


def url_variants(title):
    """Başlığın arşivde geçmiş olabileceği URL yazımları.
    Eski sözlük Türkçe karakterleri windows-1254 ile kodluyordu; yine de
    UTF-8 ve %20 biçimleri de denenir."""
    t = norm_title(title)
    out = []
    for enc in ("cp1254", "utf-8"):
        try:
            q = quote_plus(t, safe="", encoding=enc)
        except UnicodeEncodeError:
            continue
        for v in (q, q.replace("+", "%20")):
            u = "%s/show.asp?t=%s" % (HOST, v)
            if u not in out:
                out.append(u)
    return out


def _decode_bytes(raw):
    for enc in ("utf-8", "cp1254"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1254", errors="replace")


def parse_show_url(url):
    """show.asp URL'sinden (başlık, diğer parametreler) döndürür."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return None, ""
    title, extra = None, []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.lower() == "t" and title is None:
            title = _decode_bytes(unquote_to_bytes(v.replace("+", " ")))
        else:
            extra.append(_decode_bytes(unquote_to_bytes(part.replace("+", " "))))
    return title, "&".join(extra)


def fmt_ts(ts):
    if len(ts) >= 12:
        return "%s-%s-%s %s:%s" % (ts[0:4], ts[4:6], ts[6:8], ts[8:10], ts[10:12])
    return ts


def friendly_error(e):
    if isinstance(e, HTTPError):
        if e.code == 429:
            return ("archive.org çok fazla istek geldiğini bildirdi. "
                    "Bir-iki dakika bekleyip tekrar deneyin.")
        if e.code >= 500:
            return "archive.org şu an yanıt vermiyor (HTTP %d). Biraz sonra tekrar deneyin." % e.code
        return "archive.org hata döndürdü: HTTP %d" % e.code
    if isinstance(e, URLError):
        if isinstance(e.reason, ssl.SSLError):
            return ("Güvenli bağlantı sertifikası doğrulanamadı. macOS'ta Python'u "
                    "python.org'dan kurduysanız Uygulamalar > Python 3.x klasöründeki "
                    "'Install Certificates.command' dosyasını bir kez çalıştırın.")
        if isinstance(e.reason, socket.timeout):
            return "archive.org zamanında yanıt vermedi. Tekrar deneyin."
        return "Bağlantı kurulamadı: %s" % e.reason
    if isinstance(e, socket.timeout):
        return "archive.org zamanında yanıt vermedi. Tekrar deneyin."
    return "Beklenmeyen hata: %s" % e


# ------------------------------------------------------------- arşiv işlemleri

def search_archive(title, exact=True):
    """Wayback CDX API'sinde başlığı arar. Liste döndürür."""
    wanted = norm_title(title)
    found = {}
    errors = []
    for variant in url_variants(title):
        params = {
            "url": variant,
            "matchType": "prefix",
            "output": "json",
            "fl": "timestamp,original,statuscode,digest",
            "limit": "20000",
        }
        try:
            data, _ = http_get(CDX_API + "?" + urlencode(params))
        except Exception as e:
            errors.append(e)
            continue
        if not data.strip():
            continue
        try:
            rows = json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        for row in rows[1:]:
            if len(row) < 4:
                continue
            ts, original, status, digest = row[:4]
            if not (status.startswith("2") or status == "-"):
                continue
            t, extra = parse_show_url(original)
            if t is None:
                continue
            if exact and norm_title(t) != wanted:
                continue
            found[(ts, original)] = {
                "ts": ts, "original": original, "title": t,
                "extra": extra, "digest": digest,
            }
    if not found and errors and len(errors) == len(url_variants(title)):
        raise errors[0]
    results = sorted(found.values(), key=lambda r: (r["ts"], r["original"]))
    last_digest = {}
    for r in results:
        key = (norm_title(r["title"]), r["extra"])
        r["same"] = last_digest.get(key) == r["digest"]
        last_digest[key] = r["digest"]
    return results


def snapshot_url(ts, original, raw=True):
    return "https://web.archive.org/web/%s%s/%s" % (ts, "id_" if raw else "", original)


def fetch_snapshot(ts, original):
    data, header_cs = http_get(snapshot_url(ts, original))
    declared = (header_cs or "").lower()
    m = re.search(rb'charset\s*=\s*["\']?([A-Za-z0-9_\-]+)', data[:4000], re.I)
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
    if declared in ("utf-8", "utf8"):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
    # Türkçe bir site: latin1 / 8859-9 / beyan yok -> windows-1254
    return data.decode("cp1254", errors="replace")


# --------------------------------------------------------- HTML -> düz metin

class PageRenderer(HTMLParser):
    """Sayfayı (metin, etiketler) parçalarına dönüştürür."""

    BLOCK = {"p", "div", "table", "tr", "ol", "ul", "blockquote", "center",
             "form", "dl", "dt", "dd", "pre", "body", "h1", "h2", "h3",
             "h4", "h5", "h6"}
    SKIP = {"script", "style", "title", "select", "noscript", "textarea"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.segments = []
        self.links = {}
        self.skip = 0
        self.bold = 0
        self.italic = 0
        self.heading = 0
        self.pre = 0
        self.lists = []
        self.cur_link = None
        self.tail = ""

    # -- çıktı
    def _emit(self, text, tags=None):
        if not text:
            return
        if tags is None:
            tags = self._tags()
        if self.segments and self.segments[-1][1] == tags:
            self.segments[-1][0] += text
        else:
            self.segments.append([text, tags])
        self.tail = (self.tail + text)[-4:]

    def _trailing_nl(self):
        n = 0
        for ch in reversed(self.tail):
            if ch == "\n":
                n += 1
            elif ch == " ":
                continue
            else:
                break
        return n if self.tail else 2

    def _newline(self, n=1):
        have = self._trailing_nl()
        if have < n:
            self._emit("\n" * (n - have), ())

    def _tags(self):
        t = []
        if self.heading:
            t.append("h")
        if self.bold:
            t.append("b")
        if self.italic:
            t.append("i")
        if self.cur_link is not None:
            t += ["link", "L%d" % self.cur_link]
        return tuple(t)

    # -- ayrıştırma
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            if self._trailing_nl() < 2:
                self._emit("\n", ())
        elif tag == "hr":
            self._newline(1)
            self._emit("─" * 30, ("dim",))
            self._newline(1)
        elif tag in ("ol", "ul"):
            self._newline(1)
            start = 0
            try:
                start = int(a.get("start", "1")) - 1
            except ValueError:
                pass
            self.lists.append([tag, start])
        elif tag == "li":
            if self.lists and self.lists[-1][0] == "ol":
                self._newline(2)
                lst = self.lists[-1]
                try:
                    lst[1] = int(a.get("value"))
                except (TypeError, ValueError):
                    lst[1] += 1
                self._emit("%d. " % lst[1], ("num",))
            else:
                self._newline(1)
                self._emit("• ", ())
        elif tag in self.BLOCK:
            self._newline(2 if tag in ("p", "h1", "h2", "h3") else 1)
            if tag.startswith("h") and len(tag) == 2:
                self.heading += 1
            if tag == "pre":
                self.pre += 1
        elif tag in ("b", "strong"):
            self.bold += 1
        elif tag in ("i", "em"):
            self.italic += 1
        elif tag == "td":
            if self.tail and not self.tail.endswith(("\n", " ")):
                self._emit(" ", ())
        elif tag == "a":
            href = a.get("href") or ""
            if "show.asp" in href.lower():
                target, _ = parse_show_url(href)
                if target:
                    self.cur_link = len(self.links)
                    self.links[self.cur_link] = target

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("ol", "ul"):
            if self.lists:
                self.lists.pop()
            self._newline(1)
        elif tag in self.BLOCK:
            if tag.startswith("h") and len(tag) == 2:
                self.heading = max(0, self.heading - 1)
            if tag == "pre":
                self.pre = max(0, self.pre - 1)
            self._newline(2 if tag in ("p", "h1", "h2", "h3") else 1)
        elif tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("i", "em"):
            self.italic = max(0, self.italic - 1)
        elif tag == "a":
            self.cur_link = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self._emit(data)
            return
        text = re.sub(r"\s+", " ", data)
        if not self.tail or self.tail.endswith(("\n", " ")):
            text = text.lstrip(" ")
        self._emit(text)


def render_html(html_text):
    p = PageRenderer()
    try:
        p.feed(html_text)
        p.close()
    except Exception:
        pass
    segs = p.segments
    # baştaki/sondaki boşlukları temizle
    while segs and not segs[0][0].strip():
        segs.pop(0)
    if segs:
        segs[0][0] = segs[0][0].lstrip()
    while segs and not segs[-1][0].strip():
        segs.pop()
    return segs, p.links


# ------------------------------------------------------------------ arayüz

class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("1050x760")
        root.minsize(640, 420)

        self.q = queue.Queue()
        self.cache = {}
        self.rows = {}
        self.current = None
        self.page_links = {}

        base = tkfont.nametofont("TkTextFont").copy()
        base.configure(size=12)
        self.f_base = base
        self.f_bold = base.copy(); self.f_bold.configure(weight="bold")
        self.f_ital = base.copy(); self.f_ital.configure(slant="italic")
        self.f_head = base.copy(); self.f_head.configure(weight="bold", size=15)

        # üst çubuk
        top = ttk.Frame(root, padding=(10, 10, 10, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Başlık:").pack(side="left")
        self.query = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.query)
        self.entry.pack(side="left", fill="x", expand=True, padx=6)
        self.entry.bind("<Return>", lambda e: self.search())
        self.exact = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Yalnızca tam eşleşme",
                        variable=self.exact).pack(side="left", padx=6)
        self.btn = ttk.Button(top, text="Ara", command=self.search)
        self.btn.pack(side="left")

        paned = ttk.PanedWindow(root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10)

        # arşiv listesi
        lf = ttk.Frame(paned)
        cols = ("date", "title", "extra", "note")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings",
                                 selectmode="browse", height=8)
        for c, label, w, st in (("date", "Arşiv tarihi", 140, False),
                                ("title", "Başlık", 300, True),
                                ("extra", "Ek parametre", 120, False),
                                ("note", "Not", 160, False)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=st, anchor="w")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        paned.add(lf, weight=1)

        # okuyucu
        rf = ttk.Frame(paned)
        bar = ttk.Frame(rf)
        bar.pack(fill="x", pady=(6, 4))
        self.btn_prev = ttk.Button(bar, text="◀ Önceki kopya", command=lambda: self.step(-1))
        self.btn_next = ttk.Button(bar, text="Sonraki kopya ▶", command=lambda: self.step(1))
        self.btn_web = ttk.Button(bar, text="Tarayıcıda aç", command=self.open_browser)
        self.btn_prev.pack(side="left")
        self.btn_next.pack(side="left", padx=4)
        self.btn_web.pack(side="right")
        tf = ttk.Frame(rf)
        tf.pack(fill="both", expand=True)
        self.text = tk.Text(tf, wrap="word", font=self.f_base, padx=14, pady=10,
                            relief="flat", borderwidth=1, spacing1=1, spacing3=2)
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set)
        self.text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.text.tag_configure("h", font=self.f_head)
        self.text.tag_configure("b", font=self.f_bold)
        self.text.tag_configure("i", font=self.f_ital)
        self.text.tag_configure("num", font=self.f_bold, foreground="#7a4b00")
        self.text.tag_configure("dim", foreground="#888888")
        self.text.tag_configure("meta", foreground="#666666")
        self.text.tag_configure("link", foreground="#1a5fb4", underline=True)
        self.text.tag_bind("link", "<Enter>", lambda e: self.text.configure(cursor="hand2"))
        self.text.tag_bind("link", "<Leave>", lambda e: self.text.configure(cursor=""))
        self.text.tag_bind("link", "<Button-1>", self.on_link)
        paned.add(rf, weight=3)

        self.status = tk.StringVar(value="Bir başlık yazıp Enter'a basın. "
                                         "Okurken mavi (bkz:) bağlantılarına tıklayarak o başlığa geçebilirsiniz.")
        ttk.Label(root, textvariable=self.status, padding=(10, 4),
                  anchor="w").pack(fill="x")

        self.show_message("")
        self.entry.focus_set()
        root.after(100, self.poll)

    # -- arka plan işleri
    def run_bg(self, fn, on_ok, on_err):
        def worker():
            try:
                res = fn()
            except Exception as e:
                self.q.put((on_err, e))
            else:
                self.q.put((on_ok, res))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                cb, arg = self.q.get_nowait()
                cb(arg)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # -- arama
    def search(self):
        title = self.query.get().strip()
        if not title:
            return
        self.btn.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.current = None
        self.show_message("")
        self.status.set("“%s” archive.org'da aranıyor… (bu birkaç saniye sürebilir)" % title)
        exact = self.exact.get()
        self.run_bg(lambda: search_archive(title, exact),
                    lambda res: self.search_done(title, res),
                    self.search_failed)

    def search_done(self, title, results):
        self.btn.configure(state="normal")
        for i, r in enumerate(results):
            iid = str(i)
            self.rows[iid] = r
            self.tree.insert("", "end", iid=iid, values=(
                fmt_ts(r["ts"]), r["title"], r["extra"],
                "önceki kopyayla aynı" if r["same"] else ""))
        if not results:
            self.status.set("“%s” için arşiv kaydı bulunamadı." % title)
            self.show_message(
                "Bu başlık için Wayback Machine'de kayıt bulunamadı.\n\n"
                "İpuçları:\n"
                "• Yazımı kontrol edin; eski sözlükte başlıklar küçük harfliydi.\n"
                "• “Yalnızca tam eşleşme” kutusunu kaldırıp tekrar deneyin; "
                "aynı sözcükle başlayan başlıklar da listelenir.")
            return
        distinct = sum(1 for r in results if not r["same"])
        self.status.set("%d arşiv kopyası bulundu (%d farklı içerik). "
                        "Okumak için listeden birini seçin." % (len(results), distinct))
        first = next((iid for iid, r in self.rows.items() if not r["same"]), "0")
        self.tree.selection_set(first)
        self.tree.see(first)

    def search_failed(self, e):
        self.btn.configure(state="normal")
        msg = friendly_error(e)
        self.status.set("Arama başarısız.")
        self.show_message(msg)

    # -- okuma
    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        self.current = iid
        r = self.rows[iid]
        key = (r["ts"], r["original"])
        if key in self.cache:
            self.show_page(r, self.cache[key])
            return
        self.show_message("Yükleniyor: %s kopyası…" % fmt_ts(r["ts"]))
        self.run_bg(lambda: fetch_snapshot(*key),
                    lambda html_text: self.page_done(iid, key, html_text),
                    lambda e: self.page_failed(iid, e))

    def page_done(self, iid, key, html_text):
        self.cache[key] = html_text
        if self.current == iid:
            self.show_page(self.rows[iid], html_text)

    def page_failed(self, iid, e):
        if self.current == iid:
            self.show_message("Bu kopya açılamadı.\n\n" + friendly_error(e))

    def show_message(self, msg):
        self.page_links = {}
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", msg, ("meta",))
        self.text.configure(state="disabled")

    def show_page(self, r, html_text):
        segs, links = render_html(html_text)
        self.page_links = links
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", r["title"] + "\n", ("h",))
        self.text.insert("end", "Arşiv tarihi: %s   ·   %s\n\n" % (
            fmt_ts(r["ts"]), r["original"]), ("meta",))
        if not segs:
            self.text.insert("end", "(Bu kopyada okunabilir metin bulunamadı. "
                                    "“Tarayıcıda aç” ile deneyebilirsiniz.)", ("meta",))
        for text, tags in segs:
            self.text.insert("end", text, tags)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def on_link(self, _event):
        for tag in self.text.tag_names("current"):
            if tag.startswith("L") and tag[1:].isdigit():
                target = self.page_links.get(int(tag[1:]))
                if target:
                    self.query.set(target)
                    self.search()
                return "break"

    def step(self, delta):
        items = self.tree.get_children()
        if not items:
            return
        cur = self.tree.selection()
        idx = items.index(cur[0]) if cur else -1
        new = max(0, min(len(items) - 1, idx + delta))
        self.tree.selection_set(items[new])
        self.tree.see(items[new])

    def open_browser(self):
        sel = self.tree.selection()
        if sel:
            r = self.rows[sel[0]]
            webbrowser.open(snapshot_url(r["ts"], r["original"], raw=False))


def main():
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "clam" in style.theme_names() and root.tk.call("tk", "windowingsystem") == "x11":
            style.theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
