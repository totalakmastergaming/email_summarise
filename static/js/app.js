/* ================= Data ================= */
var MAILS = [];
/* Server search results (null = not searching; MAILS is the pool). */
var SEARCH_RESULTS = null;
/* User-created Gmail labels, loaded once per session. */
var LABELS_CACHE = null;
/* Global counts from the last /api/dashboard response (inbox_counts),
   when the backend provides them. Null = fall back to local counts. */
var INBOX_COUNTS = null;
/* Snapshot of the inbox/Primary page-1 threads, used for the dashboard
   "Recent mail" / "Urgent" panels and tab badges while the user pages
   through later pages (MAILS then holds the current page only). */
var PAGE1_MAILS = [];
/* Pagination: page 1 = no cursor; cursors[i] is the cursor that fetches
   page i+1. nextCursor/hasMore come from the last /api/mails response. */
var PAGE_SIZE = 30;
var pager = { page: 1, cursors: [null], nextCursor: null, hasMore: false, loading: false, error: null };
/* Last inbox page-load failure message (null = no error). Shown as an
   inline error row in the inbox list with a Retry button. */
var inboxLoadError = null;
/* Per-folder counts from the last /api/dashboard response
   (folder_counts map), when the backend provides them. Null = no
   badges (a badge must equal the folder's count, never the loaded
   page size — HOTFIX 2026-11-16). */
var FOLDER_COUNTS = null;
/* True once initBackend has painted #aiBriefText from the latest
   briefing report; renderStats must not clobber that text with the
   counts sentence (HOTFIX 2026-11-16 — the two writers disagreed:
   "15 Emails · 3 Batches" header vs "0 mails today" text). */
var AI_BRIEF_FROM_REPORT = false;
/* Attachments: total cap for one message (10 MB, per the Phase 2
   diagnostic — Gmail allows 25 MB, 10 MB bounds free-tier memory). */
var ATTACH_MAX_TOTAL = 10 * 1024 * 1024;
/* Files staged in compose / reply (File objects, never sent anywhere
   except the send call). Forward re-attached files land here too. */
var composeAttachments = [];
var replyAttachments = [];
/* Attachments the forward flow could not re-attach, shown in compose
   instead of being silently dropped: [{filename, reason}]. */
var forwardSkipped = [];
/* Draft autosave: the server draft id once one exists (null = unsaved),
   the debounce timer, and the visible save state. */
var currentDraftId = null;
var draftTimer = null;
var draftSaveState = "idle"; /* idle | saving | saved | error */
/* Current open thread (GET /api/threads/{id}) and its load state. */
var THREAD = null;
var THREAD_LOADING = false;
var THREAD_ERROR = null;
var threadLoadSeq = 0;
/* A page fetch queued while another is in flight (cursor to fetch). */
var pagePending = null;

/* Thread rows are the existing mail object plus thread_id,
   message_count and unread_count. The row's identity in the UI is its
   thread id (falling back to the message id for older payloads). */
function threadKey(m){
  if(!m) return null;
  return m.thread_id || m.threadId || m.id;
}
function rowByThreadKey(k){
  if(k == null) return null;
  var pools = [MAILS, SEARCH_RESULTS, PAGE1_MAILS];
  for(var i=0;i<pools.length;i++){
    var p = pools[i];
    if(!p) continue;
    for(var j=0;j<p.length;j++){
      if(String(threadKey(p[j])) === String(k) || String(p[j].id) === String(k)) return p[j];
    }
  }
  return null;
}
function normaliseThreadRow(m){
  if(!m) return m;
  m.thread_id = m.thread_id || m.threadId || m.id;
  m.message_count = m.message_count || m.messageCount || 1;
  if(m.unread_count != null) m.unread = m.unread_count > 0;
  else if(m.unreadCount != null){ m.unread_count = m.unreadCount; m.unread = m.unread_count > 0; }
  else m.unread_count = m.unread ? 1 : 0;
  m.starred = !!m.starred;
  m.cat = m.cat || "primary";
  m.archived = !!m.archived;
  m.trashed = !!m.trashed;
  return m;
}
function normaliseThreadMessage(msg, tid){
  if(!msg) return msg;
  msg.thread_id = msg.thread_id || msg.threadId || tid;
  msg.unread = !!msg.unread;
  msg.starred = !!msg.starred;
  msg.cat = msg.cat || "primary";
  msg.archived = false;
  msg.trashed = false;
  msg._expanded = false;
  return msg;
}
/* Dashboard pool: page-1 inbox threads when we have them, else the
   currently loaded page. */
function dashPool(){
  var p = PAGE1_MAILS.length ? PAGE1_MAILS : MAILS;
  return p.filter(function(m){ return !m.trashed && !m.archived; });
}
function countBadge(m){
  return (m.message_count && m.message_count > 1)
    ? '<span class="msg-count" title="'+m.message_count+' messages in this thread">'+m.message_count+'</span>'
    : '';
}

/* ================= Admin token + API wrapper =================
   Every /api route requires `Authorization: Bearer <ADMIN_TOKEN>` (see
   require_admin in app/routes/api.py). The token is kept in
   sessionStorage only — it never touches localStorage or the server —
   and is entered once in the Configuration view. apiFetch injects it
   on every call and surfaces the lock banner on 401/503. */
function getAdminToken(){ try { return sessionStorage.getItem("inboxcura_admin_token") || ""; } catch(e){ return ""; } }
function setAdminToken(t){ try { if(t) sessionStorage.setItem("inboxcura_admin_token", t); else sessionStorage.removeItem("inboxcura_admin_token"); } catch(e){} }
function showAuthNotice(status){
  var n = $("#authNotice"), tx = $("#authNoticeText");
  if(!n) return;
  if(tx) tx.textContent = status === 503
    ? "The server has no admin token configured (ADMIN_TOKEN is not set in Render) — the API is locked until it is set."
    : "Admin access is locked — enter your admin token to use InboxCura.";
  n.hidden = false;
}
function hideAuthNotice(){ var n=$("#authNotice"); if(n) n.hidden = true; }
function apiFetch(url, options){
  options = options || {};
  if(url.indexOf("/api/") === 0){
    options.headers = options.headers || {};
    var tok = getAdminToken();
    if(tok) options.headers["Authorization"] = "Bearer " + tok;
  }
  return fetch(url, options).then(function(res){
    if(url.indexOf("/api/") === 0){
      if(res.status === 401 || res.status === 503) showAuthNotice(res.status);
      else if(res.ok) hideAuthNotice();
    }
    return res;
  });
}
function refreshTokenStateLabel(){
  var el = $("#adminTokenState");
  if(el) el.textContent = getAdminToken() ? "Token saved for this tab." : "No token saved in this tab.";
}

/* ================= Helpers ================= */
var $ = function(s){ return document.querySelector(s); };
var $$ = function(s){ return Array.prototype.slice.call(document.querySelectorAll(s)); };
var state = { view:"dashboard", filter:"all", query:"", selectedId:null, cat:"primary", selected:{}, reading:false };

function escapeHtml(s){
  return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
/* Strip active content from model-produced HTML (backend asks the model for
   <b>/<br>/<i>/<ul>/<li> only). Anything else is dropped, not executed. */
function stripUnsafeHtml(html){
  return String(html || "")
    .replace(/<\s*(script|iframe|object|embed|form|style|link|meta|base)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*(script|iframe|object|embed|form|style|link|meta|base)[^>]*\/?>/gi, "")
    .replace(/\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/(href|src)\s*=\s*("javascript:[^"]*"|'javascript:[^']*'|javascript:[^\s>]+)/gi, '$1="#"');
}
function initials(name){
  return name.split(" ").filter(Boolean).slice(0,2).map(function(w){return w[0];}).join("").toUpperCase();
}
/* Phase 2 helpers: folders, addresses, sizes, drafts. */
var FOLDER_LABELS = { inbox: "Inbox", starred: "Starred", unread: "Unread", urgent: "Urgent", reply: "Awaiting reply", sent: "Sent", drafts: "Drafts", archive: "Archive", trash: "Trash", spam: "Spam" };
function folderLabel(f){ return FOLDER_LABELS[f] || "Inbox"; }
function isStoredFolder(f){ return f === "archive" || f === "trash" || f === "spam"; }
function formatBytes(n){
  n = Number(n) || 0;
  if(n < 1024) return n + " B";
  if(n < 1024 * 1024) return (n / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
  return (n / (1024 * 1024)).toFixed(1).replace(/\.0$/, "") + " MB";
}
function formatClock(d){
  try { return new Date(d).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch(e){ return ""; }
}
/* "a@x.com, b@y.com" -> ["a@x.com", "b@y.com"]; "" -> []. A string or
   an array is accepted, so draft objects and inputs share this. */
function parseAddressList(v){
  if(Array.isArray(v)) return v.map(function(s){ return String(s).trim(); }).filter(Boolean);
  if(typeof v !== "string") return [];
  return v.split(",").map(function(s){ return s.trim(); }).filter(Boolean);
}
function addressListToString(v){
  return parseAddressList(v).join(", ");
}
function avatarColor(name){
  var colors=["#4a4a6e","#37505f","#5b4636","#3f5a46","#5a3d55","#44506e","#6e4a3a"];
  var h=0; for(var i=0;i<name.length;i++){ h=(h*31+name.charCodeAt(i))>>>0; }
  return colors[h%colors.length];
}
function activeMails(){ return MAILS.filter(function(m){ return !m.trashed && !m.archived; }); }
function counts(){
  var a = activeMails();
  return {
    unread: a.filter(function(m){return m.unread;}).length,
    today: a.filter(function(m){return m.today;}).length,
    urgent: a.filter(function(m){return m.urgent;}).length,
    reply: a.filter(function(m){return m.needsReply;}).length
  };
}
function toast(msg){
  var t=$("#toast"); t.textContent=msg; t.classList.add("show");
  setTimeout(function(){ t.classList.remove("show"); },2400);
}
function starSvg(){
  return '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4l-5.9 3.1 1.2-6.5L2.5 9.4l6.6-.9Z"/></svg>';
}
function tagHtml(m){
  if(m.urgent) return '<span class="tag red">Urgent</span>';
  if(m.needsReply) return '<span class="tag amber">Needs reply</span>';
  return '<span class="tag grey">'+escapeHtml(m.tag)+'</span>';
}
function mailRowHtml(m){
  var tk = threadKey(m);
  return '<div class="mail-row'+(m.unread?" unread":"")+(String(state.selectedId)===String(tk)?" selected":"")+'" data-open="'+tk+'" role="button" tabindex="0">'
    + (m.unread?'<span class="unread-dot"></span>':'<span style="width:8px;flex:none"></span>')
    + '<span class="avatar" style="background:'+avatarColor(m.sender)+'">'+initials(m.sender)+'</span>'
    + '<span class="mail-main"><span class="mail-line1"><span class="mail-sender">'+escapeHtml(m.sender)+'</span>'+tagHtml(m)+countBadge(m)+'<span class="mail-time num">'+escapeHtml(m.time)+'</span></span>'
    + '<span class="mail-subject" style="display:block">'+escapeHtml(m.subject)+'</span>'
    + '<span class="mail-snip" style="display:block">'+escapeHtml(m.snippet)+'</span></span>'
    + '<button class="star-btn'+(m.starred?" on":"")+'" data-star="'+tk+'" aria-label="Star this mail">'+starSvg()+'</button></div>';
}
function miniRowHtml(m){
  var tk = threadKey(m);
  return '<button class="mini-mail" data-open="'+tk+'"><span class="avatar" style="background:'+avatarColor(m.sender)+';width:34px;height:34px;font-size:12px">'+escapeHtml(initials(m.sender))+'</span>'
    + '<span class="mini-main"><span class="mini-row"><span class="mini-sender">'+escapeHtml(m.sender)+'</span>'+countBadge(m)+'<span class="mini-time num">'+escapeHtml(m.time)+'</span></span>'
    + '<span class="mini-subject" style="display:block">'+escapeHtml(m.subject)+'</span>'
    + '<span class="mini-snip" style="display:block">'+escapeHtml(m.snippet)+'</span></span></button>';
}

/* ================= Rendering ================= */
function renderStats(){
  /* Prefer the global inbox_counts from /api/dashboard when the backend
     provides them; otherwise count the loaded mail locally. */
  var c = INBOX_COUNTS ? {
    unread: INBOX_COUNTS.unread || 0,
    today: INBOX_COUNTS.today || 0,
    urgent: INBOX_COUNTS.urgent || 0,
    reply: INBOX_COUNTS.needs_reply || 0
  } : counts();
  var pool = dashPool();
  var totalThreads = (INBOX_COUNTS && INBOX_COUNTS.total_threads != null)
    ? INBOX_COUNTS.total_threads
    : activeMails().length;
  $("#statUnread").textContent=c.unread;
  $("#statToday").textContent=c.today;
  $("#statUrgent").textContent=c.urgent;
  $("#statReply").textContent=c.reply;
  /* Sidebar badges come from the server counts only. The page pool
     must not drive them: merely browsing Archive/Trash/Spam (where
     activeMails() is empty) drove the Inbox badge to 0, and every
     folder switch repainted it from a different page (HOTFIX
     2026-11-16). When the server counts are absent, leave the last
     known badge alone instead of showing a page-local number. */
  if(INBOX_COUNTS){
    $("#navUnread").textContent=c.unread;
    $("#navUrgent").textContent=c.urgent;
  }
  renderFolderBadges();
  $("#statUnreadSub").innerHTML = c.unread>0 ? "Across "+totalThreads+" conversations in your inbox" : "Inbox zero — nothing unread";
  $("#statTodaySub").innerHTML = "Since 12:00 AM today · <b>"+pool.filter(function(m){return m.today&&m.unread;}).length+" still unread</b>";
  var firstUrgent = pool.filter(function(m){return m.urgent;})[0];
  var firstReply = pool.filter(function(m){return m.needsReply;})[0];
  $("#statUrgentSub").innerHTML = firstUrgent ? "Priority: <b>"+escapeHtml(firstUrgent.subject)+"</b>" : "Nothing marked urgent";
  $("#statReplySub").innerHTML = firstReply ? "Waiting from: <b>"+escapeHtml(firstReply.sender)+"</b>" : "No one is waiting on you";
  /* AI brief: when initBackend painted the latest briefing report
     into #aiBriefText, keep it — the counts sentence below is the
     fallback for when no briefing exists. Previously renderStats
     unconditionally overwrote the briefing on every page load, so the
     dashboard showed the report's header ("15 Emails · 3 Batches")
     next to the counts sentence ("0 mails today") (HOTFIX 2026-11-16).
     The two numbers may still legitimately differ: the header is the
     last pipeline run's totals over its configured range, while the
     counts sentence (and the stat boxes) are today-in-IST. */
  if(!AI_BRIEF_FROM_REPORT){
    $("#aiBriefText").innerHTML = "You received <b>"+c.today+" mails today</b>. <b>"+c.urgent+" are urgent</b> and <b>"+c.reply+" threads are waiting for your reply.</b>";
    var pts=[];
    pool.filter(function(m){return m.urgent;}).slice(0,3).forEach(function(m){
      pts.push("<li><span class='dot'></span><span><b>"+escapeHtml(m.sender)+":</b> "+escapeHtml((m.summary||"").split(".")[0])+".</span></li>");
    });
    var extra = pool.filter(function(m){return m.needsReply && !m.urgent;});
    if(extra.length) pts.push("<li><span class='dot'></span><span>Also awaiting a reply: <b>"+extra.map(function(m){return escapeHtml(m.sender);}).join(", ")+"</b>.</span></li>");
    $("#aiBriefPoints").innerHTML = pts.join("");
  }
}
/* Sidebar folder badges from the dashboard folder_counts map. When
   the backend does not provide it, no folder shows a badge rather
   than a made-up number. */
function renderFolderBadges(){
  var folders = ["sent", "drafts", "archive", "trash", "spam"];
  folders.forEach(function(f){
    var el = document.getElementById("nav" + f.charAt(0).toUpperCase() + f.slice(1));
    if(!el) return;
    var n = null;
    if(FOLDER_COUNTS && FOLDER_COUNTS[f] != null) n = FOLDER_COUNTS[f];
    /* No page-size fallback: showing MAILS.length as the badge made
       it read "19" while Archive was open and vanish on leaving it
       (HOTFIX 2026-11-16). No server count = no badge. */
    if(n == null || n === 0){ el.hidden = true; }
    else { el.hidden = false; el.textContent = n; }
  });
}
function filteredMails(){
  var q=state.query.trim().toLowerCase();
  var folder = currentFolder();
  /* Archived/trashed rows live in MAILS for the Archive/Trash/Spam
     folders — activeMails() would filter them out and the folder
     would always look empty. Drafts/Sent rows are not archived or
     trashed either, but they share the same unfiltered pool. */
  var base = (isStoredFolder(folder) || folder === "drafts" || folder === "sent") ? MAILS : activeMails();
  var pool = (q && SEARCH_RESULTS)
    ? SEARCH_RESULTS.filter(function(m){ return isStoredFolder(folder) ? true : (!m.trashed && !m.archived); })
    : base;
  return pool.filter(function(m){
    if(q && (m.sender+" "+m.subject+" "+m.snippet).toLowerCase().indexOf(q)===-1) return false;
    if(state.filter==="unread" && !m.unread) return false;
    if(state.filter==="urgent" && !m.urgent) return false;
    if(state.filter==="reply" && !m.needsReply) return false;
    if(state.filter==="starred" && !m.starred) return false;
    if(state.filter==="all" && m.cat!==state.cat) return false;
    return true;
  });
}
/* Gmail row + chrome */
var ICO = {
  check:'<svg viewBox="0 0 24 24"><path d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>',
  starO:'<svg viewBox="0 0 24 24"><path d="M22 9.24l-7.19-.62L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.63-7.03L22 9.24zM12 15.4l-3.76 2.27 1-4.28-3.32-2.88 4.38-.38L12 6.1l1.71 4.04 4.38.38-3.32 2.88 1 4.28L12 15.4z"/></svg>',
  starF:'<svg viewBox="0 0 24 24"><path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z"/></svg>',
  archive:'<svg viewBox="0 0 24 24"><path d="M20.54 5.23l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.15.55L3.46 5.23C3.17 5.57 3 6.02 3 6.5V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.48-.17-.93-.46-1.27zM12 17.5 6.5 12H10v-2h4v2h3.5L12 17.5zM19 5H5v-.5l1-1h12l1 1V5z"/></svg>',
  trash:'<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>',
  mailClosed:'<svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4-8 5-8-5V6l8 5 8-5v2z"/></svg>',
  mailOpen:'<svg viewBox="0 0 24 24"><path d="M21.99 8c0-.72-.37-1.35-.94-1.7L12 1 2.95 6.3C2.38 6.65 2 7.28 2 8v10c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8zM12 13 3.74 7.84 12 3l8.26 4.84L12 13z"/></svg>',
  snooze:'<svg viewBox="0 0 24 24"><path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/></svg>',
  reply:'<svg viewBox="0 0 24 24"><path d="M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z"/></svg>',
  restore:'<svg viewBox="0 0 24 24"><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.16 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/></svg>',
  forward:'<svg viewBox="0 0 24 24"><path d="M14 9V5l7 7-7 7v-4.1c-5 0-8.5 1.6-11 5.1 1-5 4-10 11-11z"/></svg>'
};
function gmRowHtml(m){
  var tk = threadKey(m);
  var sel = !!state.selected[tk];
  var folder = currentFolder();
  /* Hover actions depend on the folder: Trash/Spam/Archive rows offer
     Restore instead of Archive/Delete; draft rows offer Delete only
     (deleting a draft removes the server draft, not a Gmail thread). */
  var actions;
  if(m.draft_id){
    actions = '<button class="gm-tbtn sm" data-act="trash" data-id="'+tk+'" aria-label="Delete draft">'+ICO.trash+'</button>';
  } else if(isStoredFolder(folder)){
    actions = '<button class="gm-tbtn sm" data-act="restore" data-id="'+tk+'" aria-label="Restore to inbox">'+ICO.restore+'</button>'
      + '<button class="gm-tbtn sm" data-act="read" data-id="'+tk+'" aria-label="Mark as '+(m.unread?"read":"unread")+'">'+(m.unread?ICO.mailOpen:ICO.mailClosed)+'</button>';
  } else {
    actions = '<button class="gm-tbtn sm" data-act="archive" data-id="'+tk+'" aria-label="Archive">'+ICO.archive+'</button>'
    + '<button class="gm-tbtn sm" data-act="trash" data-id="'+tk+'" aria-label="Delete">'+ICO.trash+'</button>'
    + '<button class="gm-tbtn sm" data-act="read" data-id="'+tk+'" aria-label="Mark as '+(m.unread?"read":"unread")+'">'+(m.unread?ICO.mailOpen:ICO.mailClosed)+'</button>'
    + '<button class="gm-tbtn sm" data-act="snooze" data-id="'+tk+'" aria-label="Snooze">'+ICO.snooze+'</button>';
  }
  return '<div class="gm-row'+(m.unread?" unread":"")+(sel?" checked":"")+'" data-open="'+tk+'" role="button" tabindex="0">'
    + '<button class="gm-check sm'+(sel?" on":"")+'" data-select="'+tk+'" aria-label="Select conversation"><span class="box">'+ICO.check+'</span></button>'
    + '<button class="gm-star'+(m.starred?" on":"")+'" data-star="'+tk+'" aria-label="Star this mail">'+(m.starred?ICO.starF:ICO.starO)+'</button>'
    + '<span class="gm-avatar" style="background:'+avatarColor(m.sender)+'">'+initials(m.sender)+'</span>'
    + '<span class="gm-sender">'+escapeHtml(m.sender)+'</span>'
    + '<span class="gm-mid"><span class="gm-subj">'+escapeHtml(m.subject)+'</span>'+countBadge(m)+'<span class="gm-snip"> — '+escapeHtml(m.snippet)+'</span></span>'
    + '<span class="gm-labelchip">'+escapeHtml(m.tag)+'</span>'
    + '<span class="gm-date num">'+escapeHtml(m.time)+'</span>'
    + '<span class="gm-actions">' + actions + '</span></div>';
}
function selectedIds(){
  return Object.keys(state.selected).filter(function(k){ return state.selected[k]; });
}
function renderGmailChrome(){
  var list = filteredMails();
  /* Real window for the current page — never a total we do not know. */
  var rangeTxt;
  if(!list.length) rangeTxt = "0";
  else { var start = (pager.page - 1) * PAGE_SIZE + 1; rangeTxt = start + "–" + (start + list.length - 1); }
  var r1=$("#gmRange"), r2=$("#gmRange2");
  if(r1) r1.textContent = rangeTxt;
  if(r2) r2.textContent = rangeTxt;
  var fc=$("#gmFootCount");
  if(fc) fc.textContent = list.length ? list.length+" conversation"+(list.length>1?"s":"")+" on this page" : "No conversations in this view";
  /* Real pager state: Older only when the server says there is more,
     Newer only past page 1, both off while a page is loading. */
  var older=$("#gmOlder"), newer=$("#gmNewer");
  if(older) older.disabled = !pager.hasMore || pager.loading;
  if(newer) newer.disabled = pager.page <= 1 || pager.loading;
  /* Tab "new" badges = unread in that category (page-1 pool), same
     pattern for all five tabs. Note the pool is the Primary page-1
     snapshot, so a badge only counts what that snapshot holds — the
     backend would need per-category counts for exact tab badges. */
  var pool = dashPool();
  var soc = pool.filter(function(m){ return m.cat==="social" && m.unread; }).length;
  var pro = pool.filter(function(m){ return m.cat==="promotions" && m.unread; }).length;
  var upd = pool.filter(function(m){ return m.cat==="updates" && m.unread; }).length;
  var frm = pool.filter(function(m){ return m.cat==="forums" && m.unread; }).length;
  var bs=$("#gmNewSocial"), bp=$("#gmNewPromo"), bu=$("#gmNewUpdates"), bf=$("#gmNewForums");
  if(bs){ bs.hidden = soc===0; bs.textContent = soc+" new"; }
  if(bp){ bp.hidden = pro===0; bp.textContent = pro+" new"; }
  if(bu){ bu.hidden = upd===0; bu.textContent = upd+" new"; }
  if(bf){ bf.hidden = frm===0; bf.textContent = frm+" new"; }
  /* Category tabs only exist for the inbox; folder views hide them. */
  var tabsEl=$("#gmTabs");
  if(tabsEl) tabsEl.hidden = currentFolder() !== "inbox" || state.filter !== "all";
  $$("#gmTabs .gm-tab").forEach(function(t){
    var on = state.filter==="all" && !state.query && t.getAttribute("data-cat")===state.cat;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  /* Master checkbox + toolbar swap */
  var visIds = list.map(function(m){ return threadKey(m); });
  var allSel = visIds.length>0 && visIds.every(function(id){ return state.selected[id]; });
  var master=$("#gmMaster"); if(master) master.classList.toggle("on", allSel);
  var n = selectedIds().length;
  var barMain=$("#gmBarMain"), barSel=$("#gmBarSel");
  if(barMain) barMain.hidden = n>0;
  if(barSel) barSel.hidden = n===0;
  /* Filter bar for sidebar jumps / search */
  var fb=$("#gmFilterBar"), ft=$("#gmFilterText");
  var label = null;
  if(state.query) label = 'Search results for "'+state.query+'"';
  else if(state.filter==="starred") label = "Starred — across all tabs";
  else if(state.filter==="urgent") label = "Urgent — across all tabs";
  else if(state.filter==="unread") label = "Unread — across all tabs";
  else if(state.filter==="reply") label = "Needs reply — across all tabs";
  else if(state.filter==="sent" || state.filter==="drafts" || state.filter==="archive" || state.filter==="trash" || state.filter==="spam") label = folderLabel(state.filter);
  if(fb){ fb.hidden = !label; if(label) ft.textContent = label; }
}
function renderLists(){
  var pool = dashPool();
  var urgent = pool.filter(function(m){return m.urgent;});
  $("#urgentList").innerHTML = urgent.length ? urgent.map(miniRowHtml).join("") : '<div class="empty">Nothing urgent right now.</div>';
  $("#recentList").innerHTML = pool.slice(0,5).map(mailRowHtml).join("");
  var list = filteredMails();
  var folderEmpty = {
    sent: "No sent mail yet.",
    drafts: "No drafts yet — start a message and it will autosave here.",
    archive: "Nothing archived yet.",
    trash: "Trash is empty.",
    spam: "No spam here."
  };
  var emptyMsg = state.query ? "No mail matches your search."
    : (folderEmpty[state.filter] ? folderEmpty[state.filter]
    : (state.cat==="social" ? "No social mail here."
    : state.cat==="promotions" ? "No promotions here."
    : state.cat==="updates" ? "No updates here."
    : state.cat==="forums" ? "No forum mail here."
    : "Your Primary tab is empty."));
  var html = "";
  /* A failed page load keeps the existing list on screen and adds this
     error row on top; with nothing loaded it is the whole list area. */
  if(inboxLoadError) html += '<div class="gm-error" role="alert"><span>'+escapeHtml(inboxLoadError)+'</span><button class="gm-retry" data-retry-page>Retry</button></div>';
  if(list.length) html += list.map(gmRowHtml).join("");
  else if(!inboxLoadError) html += '<div class="gm-empty">'+emptyMsg+'</div>';
  $("#inboxList").innerHTML = html;
  renderGmailChrome();
}
function currentMail(){
  return rowByThreadKey(state.selectedId);
}
/* ================= Thread reader ================= */
function threadMessages(){ return (THREAD && THREAD.messages) || []; }
function latestThreadMessage(){ var ms = threadMessages(); return ms.length ? ms[ms.length - 1] : null; }
/* Fill a message's renderable body from body_text (preferred) or
   sanitised body_html, mirroring loadFullBody. */
function fillMessageBody(msg, bodyText, bodyHtml){
  var txt = (bodyText || "").trim();
  if(txt){
    msg._bodyText = txt;
    /* Escape first, then turn newlines into <br> — the <br> stays live
       HTML while the mail text itself cannot inject markup. */
    msg.body = txt.split(/\n{2,}/).map(function(p){ return escapeHtml(p).replace(/\n/g, "<br>"); });
    msg.bodyHtml = "";
    msg._bodyIsHtml = true;
    msg._fullLoaded = true;
    msg._bodyError = false;
  } else if(bodyHtml){
    msg.body = [];
    msg.bodyHtml = stripUnsafeHtml(bodyHtml);
    msg._bodyIsHtml = false;
    msg._fullLoaded = true;
    msg._bodyError = false;
  }
}
function messageBodyHtml(msg){
  if(msg.bodyHtml) return '<div class="gm-body-html">' + msg.bodyHtml + '</div>';
  if(msg._bodyIsHtml) return (msg.body || []).map(function(p){ return "<p>"+p+"</p>"; }).join("");
  return (msg.body || [msg.snippet || ""]).map(function(p){ return "<p>"+escapeHtml(p)+"</p>"; }).join("");
}
/* Attachments for one message, from GET /api/mails/{id} (or the
   thread payload when the backend includes them). Each row downloads
   through apiFetch so the admin-token header is sent — never a plain
   <a href>, which could not authenticate. */
function attachmentsHtml(msg){
  var atts = msg.attachments || [];
  if(!atts.length) return "";
  var rows = atts.map(function(a){
    var name = a.filename || a.name || "attachment";
    var attId = a.attachmentId || a.attachment_id || a.id;
    return '<button class="att-item" data-download-msg="'+escapeHtml(String(msg.id))+'" data-download-att="'+escapeHtml(String(attId))+'" data-download-name="'+escapeHtml(name)+'">'
      + '<svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>'
      + '<span class="att-name">'+escapeHtml(name)+'</span>'
      + '<span class="att-size">'+escapeHtml(formatBytes(a.size))+'</span></button>';
  }).join("");
  return '<div class="att-list"><h4>'+atts.length+' attachment'+(atts.length>1?"s":"")+'</h4>'+rows+'</div>'
    + (msg._attachError ? '<div class="gm-error att-error" role="alert"><span>'+escapeHtml(msg._attachError)+'</span><button class="gm-retry" data-retry-attachments="'+escapeHtml(String(msg.id))+'">Retry</button></div>' : '');
}
async function downloadAttachment(mailId, attachmentId, filename){
  try {
    var res = await apiFetch("/api/mails/" + encodeURIComponent(mailId) + "/attachments/" + encodeURIComponent(attachmentId));
    if(!res.ok) throw new Error("Download failed (" + res.status + ")");
    var blob = await res.blob();
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename || "attachment";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  } catch(e){
    toast("Couldn't download " + (filename || "attachment") + " — try again");
    /* Visible, retryable error on the message itself as well. */
    var msg = null;
    threadMessages().forEach(function(x){ if(String(x.id) === String(mailId)) msg = x; });
    if(msg){ msg._attachError = "Couldn't download " + (filename || "attachment"); renderReader(); }
  }
}
function threadMessageHtml(msg, isLatest){
  var expanded = isLatest || !!msg._expanded;
  var head = '<div class="thread-msg-head">'
    + '<span class="avatar" style="background:'+avatarColor(msg.sender || "")+'">'+initials(msg.sender || "?")+'</span>'
    + '<span class="thread-msg-who"><span class="thread-msg-name">'+escapeHtml(msg.sender || "")+'</span>'
    + '<span class="thread-msg-email">&lt;'+escapeHtml(msg.email || "")+'&gt;</span></span>'
    + '<span class="thread-msg-meta">'
    + (msg.unread ? '<span class="msg-unread">Unread</span>' : '')
    + '<span class="thread-msg-date num">'+escapeHtml(msg.time || "")+'</span>'
    + '<button class="gm-star'+(msg.starred?" on":"")+'" data-msg-star="'+escapeHtml(String(msg.id))+'" aria-label="Star this message">'+(msg.starred?ICO.starF:ICO.starO)+'</button>'
    + (expanded ? '' : '<button class="gm-textbtn" data-expand-msg="'+escapeHtml(String(msg.id))+'">Show message</button>')
    + '</span></div>';
  if(!expanded){
    return '<div class="thread-msg collapsed">'+head
      + '<div class="thread-msg-snip">'+escapeHtml(msg.subject || "")+' — '+escapeHtml(msg.snippet || "")+'</div></div>';
  }
  var bodyPart;
  if(msg._bodyError){
    bodyPart = '<div class="gm-error" role="alert"><span>Couldn\'t load this message</span><button class="gm-retry" data-retry-body="'+escapeHtml(String(msg.id))+'">Retry</button></div>';
  } else if(msg._fullLoading){
    bodyPart = '<div class="gm-empty">Loading message…</div>';
  } else if(THREAD.body_error && isLatest && !msg._fullLoaded){
    bodyPart = '<div class="gm-error" role="alert"><span>'+escapeHtml(THREAD.body_error)+'</span><button class="gm-retry" data-retry-thread="'+escapeHtml(String(THREAD.thread_id))+'">Retry</button></div>';
  } else {
    bodyPart = '<div class="gm-body">'+messageBodyHtml(msg)+'</div>';
  }
  return '<div class="thread-msg'+(isLatest?" latest":"")+'">'+head+bodyPart+attachmentsHtml(msg)+'</div>';
}
function recipientCount(msg){
  var n = 0;
  ["to", "recipients", "cc"].forEach(function(k){
    var v = msg[k];
    if(Array.isArray(v)) n = Math.max(n, v.length);
    else if(typeof v === "string" && v.trim()) n = Math.max(n, v.split(",").filter(function(s){ return s.trim(); }).length);
  });
  return n;
}
/* Show "Reply all" when the thread has other participants: more than
   one distinct sender, or a message with several recipients; when in
   doubt, whenever the thread has more than one message. */
function threadShowReplyAll(){
  var msgs = threadMessages();
  if(!msgs.length) return false;
  var senders = {};
  msgs.forEach(function(x){ if(x.sender) senders[x.sender] = 1; });
  if(Object.keys(senders).length > 1) return true;
  for(var i=0;i<msgs.length;i++){ if(recipientCount(msgs[i]) > 1) return true; }
  return msgs.length > 1;
}
async function loadThread(threadId){
  var seq = ++threadLoadSeq;
  THREAD_LOADING = true;
  THREAD_ERROR = null;
  replyAttachments = [];
  renderReader();
  try {
    var res = await apiFetch("/api/threads/" + encodeURIComponent(threadId));
    var d = await res.json().catch(function(){ return {}; });
    if(seq !== threadLoadSeq) return;
    if(!res.ok || d.status !== "success" || !d.thread) throw new Error((d && d.detail) || "Couldn't load this conversation");
    var t = d.thread;
    var tid = t.thread_id || threadId;
    THREAD = {
      thread_id: tid,
      messages: (t.messages || []).map(function(msg){ return normaliseThreadMessage(msg, tid); }),
      body_error: t.body_error || d.body_error || null
    };
    THREAD.messages.forEach(function(msg, i, arr){ msg._expanded = (i === arr.length - 1); });
    var latest = latestThreadMessage();
    if(latest){
      fillMessageBody(latest, latest.body_text, latest.body_html);
      if(!latest._fullLoaded || !latest.attachments) loadFullBody(latest);
    }
  } catch(e){
    if(seq !== threadLoadSeq) return;
    THREAD = null;
    THREAD_ERROR = (e && e.message) || "Couldn't load this conversation";
    toast(THREAD_ERROR);
  } finally {
    if(seq !== threadLoadSeq) return;
    THREAD_LOADING = false;
    renderReader();
  }
}
function renderReader(){
  var listEl=$("#gmailList"), readEl=$("#gmailRead"), box=$("#reader");
  var m = state.reading ? currentMail() : null;
  /* Archived/trashed rows can be read when the user is inside the
     Archive/Trash/Spam folder itself — only bounce out of the reader
     when such a row is opened from anywhere else. */
  if(!m || ((m.trashed || m.archived) && !isStoredFolder(currentFolder()))){
    state.reading=false;
    THREAD=null; THREAD_ERROR=null; THREAD_LOADING=false;
    if(listEl) listEl.hidden=false;
    if(readEl) readEl.hidden=true;
    return;
  }
  if(listEl) listEl.hidden=true;
  if(readEl) readEl.hidden=false;
  /* Restore is only offered inside Archive/Trash/Spam. */
  var rstBtn=$("#gmReadRestore");
  if(rstBtn) rstBtn.hidden = !isStoredFolder(currentFolder());
  var tkey = threadKey(m);
  var list=filteredMails();
  var idx=-1;
  for(var i=0;i<list.length;i++){ if(String(threadKey(list[i]))===String(tkey)){ idx=i; break; } }
  var rr=$("#gmReadRange");
  if(rr) rr.textContent = idx>-1 ? (idx+1)+" of "+list.length : "";
  var pv=$("#gmPrev"), nx=$("#gmNext");
  if(pv) pv.disabled = idx<=0;
  if(nx) nx.disabled = idx<0 || idx>=list.length-1;
  if(THREAD_ERROR){
    box.innerHTML =
      '<div class="gm-subject-row"><h2>'+escapeHtml(m.subject)+'</h2></div>'
      + '<div class="gm-error" role="alert"><span>'+escapeHtml(THREAD_ERROR)+'</span><button class="gm-retry" data-retry-thread="'+escapeHtml(String(tkey))+'">Retry</button></div>';
    return;
  }
  if(THREAD_LOADING || !THREAD || String(THREAD.thread_id)!==String(tkey)){
    box.innerHTML =
      '<div class="gm-subject-row"><h2>'+escapeHtml(m.subject)+'</h2></div>'
      + '<div class="gm-empty">Loading conversation…</div>';
    return;
  }
  var msgs = THREAD.messages;
  if(!msgs.length){
    box.innerHTML =
      '<div class="gm-subject-row"><h2>'+escapeHtml(m.subject)+'</h2></div>'
      + '<div class="gm-empty">No messages in this conversation.</div>';
    return;
  }
  var latest = msgs[msgs.length-1];
  box.innerHTML =
    '<div class="gm-subject-row"><h2>'+escapeHtml(latest.subject || m.subject)+'</h2><span class="gm-labelchip">'+escapeHtml(folderLabel(currentFolder()))+'</span><span class="gm-labelchip">'+escapeHtml(m.tag)+'</span>'+countBadge(m)+'</div>'
    + '<div class="gm-ai"><h3><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/></svg>AI summary</h3><p>'+escapeHtml(m.summary)+'</p></div>'
    + msgs.map(function(msg, i){ return threadMessageHtml(msg, i===msgs.length-1); }).join("")
    + '<div class="gm-reply-btns"><button class="gm-pill" id="gmReplyBtn">'+ICO.reply+'Reply</button>'
    + '<button class="gm-pill" id="gmForwardBtn">'+ICO.forward+'Forward</button></div>'
    + '<div class="gm-replybox" id="gmReplyBox" hidden>'
    + '<div class="cc-row"><button class="gm-textbtn" type="button" id="rCcBccToggle" aria-expanded="false">Cc / Bcc</button></div>'
    + '<input class="reply-ccbcc" id="rCc" type="text" placeholder="Cc" aria-label="Cc recipients" hidden />'
    + '<input class="reply-ccbcc" id="rBcc" type="text" placeholder="Bcc" aria-label="Bcc recipients" hidden />'
    + (threadShowReplyAll() ? '<label class="gm-replyall"><input type="checkbox" id="replyAllCheck"> Reply all</label>' : '')
    + '<textarea id="replyText" placeholder="Reply to '+escapeHtml(latest.sender)+'…"></textarea>'
    + '<div class="attach-row"><button class="gm-textbtn" type="button" id="rAttachBtn">Attach files</button><span class="attach-total" id="rAttachTotal"></span><input type="file" id="rAttachInput" multiple hidden /></div>'
    + '<div id="rAttachList"></div>'
    + '<div class="compose-note" id="rAttachError" hidden></div>'
    + '<div class="gm-reply-actions"><button class="gm-send" id="replyMailto">Send</button>'
    + '<button class="gm-textbtn" id="replyDraft">Draft with AI</button>'
    + '<button class="gm-textbtn" id="replyCopy">Copy text</button></div></div>';
  function openReplyBox(){
    var rb=$("#gmReplyBox"); if(rb) rb.hidden=false;
    var rt=$("#replyText"); if(rt) rt.focus();
  }
  var ccTog=$("#rCcBccToggle");
  if(ccTog) ccTog.addEventListener("click",function(){
    var cc=$("#rCc"), bcc=$("#rBcc");
    var show = !cc || cc.hidden;
    if(cc) cc.hidden = !show;
    if(bcc) bcc.hidden = !show;
    ccTog.setAttribute("aria-expanded", show ? "true" : "false");
    if(show && cc) cc.focus();
  });
  var rAttBtn=$("#rAttachBtn"), rAttInput=$("#rAttachInput");
  if(rAttBtn && rAttInput){
    rAttBtn.addEventListener("click",function(){ rAttInput.click(); });
    rAttInput.addEventListener("change",function(){
      addAttachFiles(rAttInput.files, "reply");
      rAttInput.value = "";
    });
  }
  renderAttachList("reply");
  var rb1=$("#gmReplyBtn"); if(rb1) rb1.addEventListener("click",openReplyBox);
  var fw=$("#gmForwardBtn");
  if(fw) fw.addEventListener("click",async function(){
    /* Forward opens the compose box prefilled (sent via the Gmail API
       on submit), instead of a mailto: draft. The original attachments
       are re-attached from Gmail where they fit the 10 MB cap; any
       that do not fit (or cannot be downloaded) are named in compose
       instead of being silently dropped. */
    var to=$("#cTo"), sub=$("#cSubject"), bod=$("#cBody");
    startFreshCompose();
    if(to) to.value="";
    if(sub) sub.value="Fwd: "+(latest.subject || m.subject);
    if(bod) bod.value="\n\n---------- Forwarded message ----------\nFrom: "+latest.sender+" <"+latest.email+">\nSubject: "+(latest.subject || m.subject)+"\n\n"+(latest._bodyText || latest.snippet || "");
    forwardSkipped = [];
    var atts = latest.attachments || [];
    if(!atts.length && !latest._fullLoaded){
      await loadFullBody(latest);
      atts = latest.attachments || [];
    }
    for(var ai=0; ai<atts.length; ai++){
      var a = atts[ai];
      var aname = a.filename || a.name || "attachment";
      var asize = Number(a.size) || 0;
      var curTotal = composeAttachments.reduce(function(s,f){ return s + (f.size||0); }, 0);
      if(asize && curTotal + asize > ATTACH_MAX_TOTAL){
        forwardSkipped.push({ filename: aname, reason: "over size cap" });
        continue;
      }
      try {
        var r = await apiFetch("/api/mails/" + encodeURIComponent(latest.id) + "/attachments/" + encodeURIComponent(a.attachmentId || a.attachment_id || a.id));
        if(!r.ok) throw new Error("download failed");
        var blob = await r.blob();
        composeAttachments.push(new File([blob], aname, { type: a.mimeType || blob.type || "application/octet-stream" }));
      } catch(e){
        forwardSkipped.push({ filename: aname, reason: "couldn't download" });
      }
    }
    renderAttachList("compose");
    showForwardNote();
    openCompose();
  });
  var draftBtn=$("#replyDraft");
  if(draftBtn) draftBtn.addEventListener("click",function(){
    var rt=$("#replyText"); if(rt) rt.value = draftReply(latest);
    toast("Draft added — edit it before sending");
  });
  var mailBtn=$("#replyMailto");
  if(mailBtn) mailBtn.addEventListener("click",async function(){
    var v=$("#replyText").value.trim();
    if(!v){ toast("Write a reply first"); return; }
    mailBtn.disabled=true; mailBtn.textContent="Sending…";
    try {
      var allBox=$("#replyAllCheck");
      var ccList = parseAddressList($("#rCc") ? $("#rCc").value : "");
      var bccList = parseAddressList($("#rBcc") ? $("#rBcc").value : "");
      var url = "/api/threads/" + encodeURIComponent(String(tkey)) + "/reply";
      var res;
      if(replyAttachments.length){
        /* Attachments ride as FormData (cc/bcc as repeated fields);
           JSON is used when there is nothing to upload. */
        var fd = new FormData();
        fd.append("body", v);
        fd.append("reply_all", (allBox && allBox.checked) ? "true" : "false");
        ccList.forEach(function(a){ fd.append("cc", a); });
        bccList.forEach(function(a){ fd.append("bcc", a); });
        replyAttachments.forEach(function(f){ fd.append("attachments", f, f.name); });
        res = await apiFetch(url, { method: "POST", body: fd });
      } else {
        var payload = { body: v, reply_all: !!(allBox && allBox.checked) };
        if(ccList.length) payload.cc = ccList;
        if(bccList.length) payload.bcc = bccList;
        res = await apiFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      }
      var d = await res.json().catch(function(){ return {}; });
      if(res.ok){ toast("Reply sent"); replyAttachments=[]; var rb=$("#gmReplyBox"); if(rb) rb.hidden=true; var rt=$("#replyText"); if(rt) rt.value=""; loadThread(String(tkey)); }
      else { toast(d.detail || "Couldn't send the reply"); }
    } catch(e){ toast("Couldn't send the reply — check connection"); }
    finally { mailBtn.disabled=false; mailBtn.textContent="Send"; }
  });
  var copyBtn=$("#replyCopy");
  if(copyBtn) copyBtn.addEventListener("click",function(){
    var v=$("#replyText").value;
    if(!v){ toast("Write or draft a reply first"); return; }
    if(navigator.clipboard) navigator.clipboard.writeText(v);
    toast("Reply copied");
  });
}
function draftReply(m){
  var firstName = (m.sender || "").split(" ")[0] || "there";
  return "Hi "+firstName+",\n\nThank you for your email. I have noted the details and will follow up shortly.\n\nRegards";
}
/* ================= Local Storage State Persistence ================= */
function saveLocalState(){
  try {
    var stateData = { starred:{}, read:{}, archived:{}, trashed:{} };
    var seen = {};
    [MAILS, PAGE1_MAILS, SEARCH_RESULTS].forEach(function(pool){
      if(!pool) return;
      pool.forEach(function(m){
        if(seen[m.id]) return;
        seen[m.id] = true;
        stateData.starred[m.id] = !!m.starred;
        stateData.read[m.id] = !m.unread;
        stateData.archived[m.id] = !!m.archived;
        stateData.trashed[m.id] = !!m.trashed;
      });
    });
    localStorage.setItem("inboxcura_mail_state", JSON.stringify(stateData));
  } catch(e){}
}

function applyLocalState(){
  /* Only starred/read are restored from localStorage. Archived/trashed
     are NOT restored: the backend now filters those out server-side
     (/api/mails excludes them), so a stale local flag could only hide
     mail that is actually still in the Gmail inbox. */
  try {
    var raw = localStorage.getItem("inboxcura_mail_state");
    if(!raw) return;
    var s = JSON.parse(raw);
    [MAILS, PAGE1_MAILS, SEARCH_RESULTS].forEach(function(pool){
      if(!pool) return;
      pool.forEach(function(m){
        if(s.starred && s.starred[m.id] !== undefined) m.starred = !!s.starred[m.id];
        if(s.read && s.read[m.id] !== undefined) m.unread = !s.read[m.id];
      });
    });
  } catch(e){}
}

function renderAll(){
  renderStats();
  renderLists();
  renderReader();
  saveLocalState();
}

/* ================= Navigation ================= */
function showView(name){
  state.view=name;
  $$(".view").forEach(function(v){ v.classList.remove("active"); });
  var el=$("#view-"+name); if(el) el.classList.add("active");
  var content=document.querySelector(".content");
  if(content) content.classList.toggle("inbox-wide", name==="inbox");
  $$(".nav-item[data-view]").forEach(function(n){ n.classList.toggle("active", n.getAttribute("data-view")===name); });
  $("#sidebar").classList.remove("open"); $("#navScrim").classList.remove("open");
  window.scrollTo(0,0);
  if(name === "config"){ loadConfig(); loadSummaryJobs(); }
  if(name === "api-keys"){ loadApiKeys(); }
}
$$(".nav-item[data-view]").forEach(function(n){
  n.addEventListener("click",function(){
    if(n.getAttribute("data-view")==="inbox"){ state.reading=false; renderAll(); }
    showView(n.getAttribute("data-view"));
  });
});
$$("[data-goto]").forEach(function(b){
  b.addEventListener("click",function(){
    var f=b.getAttribute("data-goto-filter");
    if(f) setFilter(f);
    if(b.getAttribute("data-goto")==="inbox"){ state.reading=false; renderReader(); }
    showView(b.getAttribute("data-goto"));
  });
});
$$("[data-filter-jump]").forEach(function(n){
  n.addEventListener("click",function(){ state.reading=false; setFilter(n.getAttribute("data-filter-jump")); renderReader(); showView("inbox"); });
});
$("#menuBtn").addEventListener("click",function(){ $("#sidebar").classList.add("open"); $("#navScrim").classList.add("open"); });
$("#navScrim").addEventListener("click",function(){ $("#sidebar").classList.remove("open"); $("#navScrim").classList.remove("open"); });

function setFilter(f){
  state.filter=f;
  state.selected={};
  /* HOTFIX 2026-11-16: drop the previous folder's rows NOW, before the
     fetch lands. filteredMails() has no folder predicate — folder
     scoping is server-side only — so painting from the stale pool
     showed Sent's rows under the Drafts header until the new fetch
     arrived (or forever, if it failed). */
  MAILS = [];
  SEARCH_RESULTS = null;
  /* Folder change resets pagination to page 1 and reloads from the server. */
  resetPager();
  renderLists();
  fetchPage(null);
}
function setCategory(c){
  state.cat=c;
  state.filter="all";
  state.selected={};
  /* Same stale-pool drop as setFilter (HOTFIX 2026-11-16): the old
     tab's rows must not paint under the new tab while its fetch is
     in flight. */
  MAILS = [];
  SEARCH_RESULTS = null;
  /* Category-tab change resets pagination to page 1 and reloads. */
  resetPager();
  renderLists();
  fetchPage(null);
}

/* ================= Pagination + thread state ================= */
function currentFolder(){
  if(state.filter==="starred") return "starred";
  if(state.filter==="unread") return "unread";
  if(state.filter==="urgent") return "urgent";
  if(state.filter==="reply") return "reply";
  if(state.filter==="sent") return "sent";
  if(state.filter==="drafts") return "drafts";
  if(state.filter==="archive") return "archive";
  if(state.filter==="trash") return "trash";
  if(state.filter==="spam") return "spam";
  return "inbox";
}
function resetPager(){
  pager.page = 1;
  pager.cursors = [null];
  pager.nextCursor = null;
  pager.hasMore = false;
  pager.error = null;
  inboxLoadError = null;
}
function pageParams(){
  var q = state.query.trim();
  var params = { folder: currentFolder(), limit: PAGE_SIZE };
  /* The category tab only scopes the inbox folder; other folders
     (starred/unread/urgent/reply) span all categories. */
  if(params.folder === "inbox" && !q) params.cat = state.cat;
  if(q) params.q = q;
  return params;
}
function buildMailsUrl(cursor){
  var p = pageParams();
  var qs = "limit=" + p.limit + "&folder=" + encodeURIComponent(p.folder);
  if(p.cat) qs += "&cat=" + encodeURIComponent(p.cat);
  if(p.q) qs += "&q=" + encodeURIComponent(p.q);
  if(cursor) qs += "&cursor=" + encodeURIComponent(cursor);
  return "/api/mails?" + qs;
}
/* Drafts folder: server drafts (GET /api/drafts) carry the fields a
   draft needs to be restored — to/cc/bcc/subject/body — which the
   /api/mails thread rows do not, so the folder lists drafts directly
   instead of going through /api/mails?folder=drafts. Rows are shaped
   like mail rows so the pager, selection and empty state all work. */
function draftRowFromDraft(d){
  var id = d.id != null ? d.id : d.draft_id;
  var body = d.body || "";
  var when = d.updated_at || d.created_at || "";
  var t = "";
  try { t = when ? new Date(when).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""; } catch(e){}
  return {
    id: "draft:" + id,
    draft_id: id,
    _draft: d,
    thread_id: "draft:" + id,
    sender: "Draft",
    email: addressListToString(d.to) || "",
    subject: d.subject || "(No subject)",
    snippet: body.replace(/\s+/g, " ").slice(0, 120),
    body_text: body,
    time: t,
    tag: "Draft",
    cat: "primary",
    unread: false,
    unread_count: 0,
    starred: false,
    archived: false,
    trashed: false,
    message_count: 1
  };
}
async function fetchDraftsPage(cursor){
  if(pager.loading){ pagePending = cursor; return "queued"; }
  pager.loading = true;
  /* HOTFIX 2026-11-16: remember which view this fetch belongs to; a
     landing after the user switched views must not overwrite the new
     view's list (or repaint the old view's error into it). A fresh
     attempt also clears the previous error while it loads. */
  var fetchFolder = currentFolder(), fetchQuery = state.query;
  pager.error = null;
  inboxLoadError = null;
  renderGmailChrome();
  var status = 0;
  try {
    var qs = "limit=" + PAGE_SIZE;
    if(cursor) qs += "&cursor=" + encodeURIComponent(cursor);
    var res = await apiFetch("/api/drafts?" + qs);
    status = res.status;
    var d = await res.json().catch(function(){ return {}; });
    var list = null;
    if(Array.isArray(d)) list = d;
    else if(Array.isArray(d.drafts)) list = d.drafts;
    else if(Array.isArray(d.mails)) list = d.mails;
    if(!res.ok || (d.status && d.status !== "success") || list === null){
      throw new Error((d && d.detail) || ("Couldn't load drafts (" + res.status + ")"));
    }
    if(currentFolder() !== fetchFolder || state.query !== fetchQuery){
      return "stale"; /* View changed mid-flight; the queued fetch owns the list. */
    }
    MAILS = list.map(draftRowFromDraft);
    SEARCH_RESULTS = null;
    pager.nextCursor = d.next_cursor || null;
    pager.hasMore = !!d.has_more;
    pager.error = null;
    inboxLoadError = null;
    renderAll();
    return true;
  } catch(e){
    if(status === 401 || status === 503){
      pager.error = null;
      inboxLoadError = null;
      renderAll();
      return false;
    }
    if(currentFolder() !== fetchFolder || state.query !== fetchQuery){
      return "stale"; /* Stale failure: never paint it into the new view. */
    }
    var msg = (e && e.message) || "Couldn't load drafts";
    pager.error = msg;
    inboxLoadError = msg;
    toast(msg + " — try again");
    renderLists();
    return false;
  } finally {
    pager.loading = false;
    renderGmailChrome();
    if(pagePending !== null){ var c = pagePending; pagePending = null; fetchPage(c); }
  }
}
async function fetchPage(cursor){
  /* The Drafts folder lists server drafts, not mail threads (see
     fetchDraftsPage) — except while searching, which goes through the
     mail search path like every other folder. */
  if(currentFolder() === "drafts" && !state.query.trim()) return fetchDraftsPage(cursor);
  /* While a page is loading, queue the newest request instead of
     racing it — the queue drains in order once the flight lands. */
  if(pager.loading){ pagePending = cursor; return "queued"; }
  pager.loading = true;
  /* HOTFIX 2026-11-16: same stale-landing guard as fetchDraftsPage. */
  var fetchFolder = currentFolder(), fetchCat = state.cat, fetchQuery = state.query;
  pager.error = null;
  inboxLoadError = null;
  renderGmailChrome();
  var isSearch = !!state.query.trim();
  var status = 0;
  try {
    var res = await apiFetch(buildMailsUrl(cursor));
    status = res.status;
    var d = await res.json().catch(function(){ return {}; });
    if(!res.ok || d.status !== "success" || !Array.isArray(d.mails)){
      throw new Error((d && d.detail) || ("Couldn't load mail (" + res.status + ")"));
    }
    if(currentFolder() !== fetchFolder || state.cat !== fetchCat || state.query !== fetchQuery){
      return "stale"; /* View changed mid-flight; the queued fetch owns the list. */
    }
    var rows = d.mails.map(normaliseThreadRow);
    if(isSearch){ SEARCH_RESULTS = rows; }
    else { MAILS = rows; SEARCH_RESULTS = null; }
    pager.nextCursor = d.next_cursor || null;
    pager.hasMore = !!d.has_more;
    pager.error = null;
    inboxLoadError = null;
    var p = pageParams();
    if(pager.page === 1 && !isSearch && p.folder === "inbox" && p.cat === "primary") PAGE1_MAILS = rows.slice();
    applyLocalState();
    renderAll();
    return true;
  } catch(e){
    if(status === 401 || status === 503){
      /* The auth notice already explains this; no inbox error box. */
      pager.error = null;
      inboxLoadError = null;
      renderAll();
      return false;
    }
    if(currentFolder() !== fetchFolder || state.cat !== fetchCat || state.query !== fetchQuery){
      return "stale"; /* Stale failure: never paint it into the new view. */
    }
    var msg = (e && e.message) || "Couldn't load this page";
    pager.error = msg;
    inboxLoadError = msg;
    toast(msg + " — try again");
    renderLists();
    return false;
  } finally {
    pager.loading = false;
    renderGmailChrome();
    if(pagePending !== null){ var c = pagePending; pagePending = null; fetchPage(c); }
  }
}
async function goOlder(){
  if(pager.loading || !pager.hasMore || !pager.nextCursor) return;
  var c = pager.nextCursor;
  pager.cursors.push(c);
  pager.page++;
  renderGmailChrome();
  var ok = await fetchPage(c);
  if(ok === false){ pager.cursors.pop(); pager.page--; renderGmailChrome(); }
}
async function goNewer(){
  if(pager.loading || pager.page <= 1) return;
  var popped = pager.cursors.pop();
  pager.page--;
  renderGmailChrome();
  var ok = await fetchPage(pager.cursors[pager.cursors.length - 1]);
  if(ok === false){ pager.cursors.push(popped); pager.page++; renderGmailChrome(); }
}

function threadStateToBackend(id, payload, quiet){
  /* POST /api/threads/{id}/state. Returns true only when Gmail
     accepted the change, so callers flip local state on success only. */
  return apiFetch("/api/threads/" + encodeURIComponent(id) + "/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function(res){
    return res.json().catch(function(){ return {}; }).then(function(d){
      if(!res.ok){
        if(!quiet) toast(d.detail || "Couldn't sync that change with Gmail");
        return false;
      }
      if(d.synced_gmail === false){
        if(!quiet) toast("Gmail didn't accept that change — nothing was updated");
        return false;
      }
      return true;
    });
  }).catch(function(){ if(!quiet) toast("Couldn't reach the server to sync that change"); return false; });
}
async function threadBulkState(threadKeys, payload){
  /* No bulk thread endpoint exists, so the selection toolbar applies
     the thread state call per thread and returns only the ids Gmail
     accepted — callers flip local state for those ids only. */
  var okIds = [];
  for(var i=0;i<threadKeys.length;i++){
    var ok = await threadStateToBackend(threadKeys[i], payload, true);
    if(ok) okIds.push(threadKeys[i]);
  }
  return okIds;
}

function syncStateToBackend(id, payload){
  /* Returns a promise of true/false so callers only flip local state
     when Gmail actually accepted the change. */
  return apiFetch("/api/mails/" + id + "/state", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  }).then(function(res){
    if(!res.ok){
      return res.json().catch(function(){ return {}; }).then(function(d){
        toast(d.detail || "Couldn't sync that change with Gmail");
        return false;
      });
    }
    return true;
  }).catch(function(err){ console.log("State sync note:", err); toast("Couldn't reach the server to sync that change"); return false; });
}

/* Delegated clicks: open mail + star + Gmail row controls + thread
   expand/retry controls */
async function loadFullBody(m){
  /* Fetch one message's real body (GET /api/mails/{id}) — used for the
     latest message when the thread payload has no body, and when an
     earlier message is expanded. A failure sets _bodyError, which the
     reader renders as a visible error box with a Retry button. The
     same fetch also carries the message's attachments, so a message
     whose body is already known is still fetched once while its
     attachments are unknown. */
  if(!m || m._fullLoading || (m._fullLoaded && m.attachments)) return;
  m._fullLoading = true;
  m._bodyError = false;
  try {
    var res = await apiFetch("/api/mails/" + m.id);
    var d = await res.json();
    if(res.ok && d.status === "success" && d.mail){
      fillMessageBody(m, d.mail.body_text, d.mail.body_html);
      if(!m._fullLoaded) m._bodyError = true;
      if(d.mail.label_ids) m.labels = d.mail.label_ids.join(",");
      /* Attachments arrive with the full message (contract:
         mail.attachments [{filename, mimeType, size, attachmentId}]). */
      if(Array.isArray(d.mail.attachments)) m.attachments = d.mail.attachments;
    } else {
      m._bodyError = true;
    }
  } catch(e){
    m._bodyError = true;
  } finally {
    m._fullLoading = false;
    if(state.reading) renderReader();
  }
}
function openMail(id){
  var row = rowByThreadKey(id);
  /* A draft row opens in compose, restored from the server draft —
     never in the thread reader. */
  if(row && row.draft_id){
    openDraft(row);
    return;
  }
  state.selectedId = id;
  state.reading = true;
  THREAD = null;
  THREAD_ERROR = null;
  THREAD_LOADING = false;
  if(row && row.unread){
    /* Mark the whole thread read; on sync failure restore the unread
       state instead of showing a read thread Gmail rejected. */
    var prevUnread = row.unread, prevCount = row.unread_count;
    row.unread = false;
    row.unread_count = 0;
    threadStateToBackend(threadKey(row), { is_unread: false }, true).then(function(ok){
      if(!ok){
        row.unread = prevUnread;
        row.unread_count = prevCount;
        renderAll();
        toast("Couldn't mark as read in Gmail — still unread");
      } else {
        renderAll();
      }
    });
  }
  renderAll();
  showView("inbox");
  if(row) loadThread(threadKey(row));
  else {
    THREAD_ERROR = "Couldn't load this conversation";
    renderReader();
  }
}
function mailById(id){
  return rowByThreadKey(id);
}
function rowAction(act, m){
  if(!m) return;
  var tk = threadKey(m);
  /* Draft rows: Delete removes the server draft. No Gmail state call
     applies to a draft. */
  if(m.draft_id){
    if(act === "trash"){
      apiFetch("/api/drafts/" + encodeURIComponent(m.draft_id), { method: "DELETE" }).then(function(res){
        if(!res.ok){ toast("Couldn't delete that draft"); return; }
        var i = MAILS.indexOf(m); if(i > -1) MAILS.splice(i, 1);
        delete state.selected[tk];
        renderAll();
        toast("Draft deleted");
      }).catch(function(){ toast("Couldn't delete that draft — check connection"); });
    }
    return;
  }
  if(act==="restore"){
    /* Un-trash / un-spam / un-archive via the existing thread state
       endpoint (is_trash/is_spam/is_archived false). The row leaves
       this folder only once Gmail accepts the change. */
    var folder = currentFolder();
    var payload = folder === "trash" ? { is_trash: false } : folder === "spam" ? { is_spam: false } : { is_archived: false };
    threadStateToBackend(tk, payload).then(function(ok){
      if(!ok) return;
      var i = MAILS.indexOf(m); if(i > -1) MAILS.splice(i, 1);
      if(String(threadKey(currentMail()))===String(tk)) state.reading=false;
      delete state.selected[tk]; renderAll();
      toast("Conversation restored to inbox");
    });
    return;
  }
  if(act==="archive"){
    threadStateToBackend(tk, { is_archived: true }).then(function(ok){
      if(!ok) return;
      m.archived=true; if(String(threadKey(currentMail()))===String(tk)) state.reading=false; delete state.selected[tk]; renderAll();
      toast("Conversation archived");
    });
    return;
  }
  else if(act==="trash"){
    threadStateToBackend(tk, { is_trash: true }).then(function(ok){
      if(!ok) return;
      m.trashed=true; if(String(threadKey(currentMail()))===String(tk)) state.reading=false; delete state.selected[tk]; renderAll();
      toast("Conversation deleted");
    });
    return;
  }
  else if(act==="spam"){
    threadStateToBackend(tk, { is_spam: true }).then(function(ok){
      if(!ok) return;
      m.archived=true; if(String(threadKey(currentMail()))===String(tk)) state.reading=false; delete state.selected[tk]; renderAll();
      toast("Reported as spam");
    });
    return;
  }
  else if(act==="read"){
    /* Flip local state only after Gmail confirms — never a
       success-looking UI for a rejected change. */
    var target = !m.unread;
    threadStateToBackend(tk, { is_unread: target }).then(function(ok){
      if(!ok) return;
      m.unread = target;
      m.unread_count = target ? Math.max(1, m.unread_count || 0) : 0;
      delete state.selected[tk];
      renderAll();
      toast(target ? "Marked as unread" : "Marked as read");
    });
    return;
  }
  else if(act==="snooze"){ toast("Snooze isn\u2019t available yet \u2014 this mail stays in your inbox"); }
  delete state.selected[tk];
  renderAll();
}
document.addEventListener("click",function(e){
  var dl=e.target.closest ? e.target.closest("[data-download-att]") : null;
  if(dl){
    e.stopPropagation();
    var btn = dl;
    btn.disabled = true;
    downloadAttachment(dl.getAttribute("data-download-msg"), dl.getAttribute("data-download-att"), dl.getAttribute("data-download-name")).finally(function(){ btn.disabled = false; });
    return;
  }
  var ratt=e.target.closest ? e.target.closest("[data-retry-attachments]") : null;
  if(ratt){
    e.stopPropagation();
    var rmid=ratt.getAttribute("data-retry-attachments");
    var rmsg=null;
    threadMessages().forEach(function(x){ if(String(x.id)===rmid) rmsg=x; });
    if(rmsg){ rmsg._attachError=null; rmsg._fullLoaded=false; loadFullBody(rmsg); renderReader(); }
    return;
  }
  var rmAtt=e.target.closest ? e.target.closest("[data-remove-att]") : null;
  if(rmAtt){
    e.stopPropagation();
    var idx=parseInt(rmAtt.getAttribute("data-remove-att"), 10);
    var which=rmAtt.getAttribute("data-att-list") || "compose";
    if(which === "reply") replyAttachments.splice(idx, 1);
    else { composeAttachments.splice(idx, 1); scheduleDraftAutosave(); }
    renderAttachList(which);
    return;
  }
  var mstar=e.target.closest ? e.target.closest("[data-msg-star]") : null;
  if(mstar){
    /* Message-level star inside an expanded thread message (single-mail
       endpoint); the thread row's star follows "any message starred". */
    e.stopPropagation();
    var mid=mstar.getAttribute("data-msg-star");
    var msg=null;
    threadMessages().forEach(function(x){ if(String(x.id)===mid) msg=x; });
    if(msg){
      var mnext=!msg.starred;
      syncStateToBackend(mid, { is_starred: mnext }).then(function(ok){
        if(!ok) return;
        msg.starred=mnext;
        var row=currentMail();
        if(row){
          var any=false;
          threadMessages().forEach(function(x){ if(x.starred) any=true; });
          row.starred=any;
        }
        renderAll();
      });
    }
    return;
  }
  var exp=e.target.closest ? e.target.closest("[data-expand-msg]") : null;
  if(exp){
    e.stopPropagation();
    var eid=exp.getAttribute("data-expand-msg");
    var emsg=null;
    threadMessages().forEach(function(x){ if(String(x.id)===eid) emsg=x; });
    if(emsg){
      emsg._expanded=true;
      if(emsg.body_text || emsg.body_html) fillMessageBody(emsg, emsg.body_text, emsg.body_html);
      if(!emsg._fullLoaded || !emsg.attachments) loadFullBody(emsg);
      renderReader();
    }
    return;
  }
  var rbody=e.target.closest ? e.target.closest("[data-retry-body]") : null;
  if(rbody){
    e.stopPropagation();
    var bid=rbody.getAttribute("data-retry-body");
    var bmsg=null;
    threadMessages().forEach(function(x){ if(String(x.id)===bid) bmsg=x; });
    if(bmsg){ bmsg._bodyError=false; bmsg._fullLoaded=false; loadFullBody(bmsg); renderReader(); }
    return;
  }
  var rthread=e.target.closest ? e.target.closest("[data-retry-thread]") : null;
  if(rthread){
    e.stopPropagation();
    loadThread(rthread.getAttribute("data-retry-thread"));
    return;
  }
  var rpage=e.target.closest ? e.target.closest("[data-retry-page]") : null;
  if(rpage){
    e.stopPropagation();
    fetchPage(pager.cursors[pager.cursors.length - 1]);
    return;
  }
  var star=e.target.closest ? e.target.closest("[data-star]") : null;
  if(star){
    /* Thread-level star: flip only after Gmail confirms. Starring
       applies to the latest message; unstarring clears the thread. */
    e.stopPropagation();
    var tk=star.getAttribute("data-star");
    var row=rowByThreadKey(tk);
    if(row && row.draft_id) return; /* Draft rows cannot be starred. */
    if(row){
      var next=!row.starred;
      threadStateToBackend(tk, { is_starred: next }).then(function(ok){
        if(!ok) return;
        row.starred=next;
        if(THREAD && String(THREAD.thread_id)===String(tk)){
          THREAD.messages.forEach(function(x, i, arr){
            if(next){ if(i===arr.length-1) x.starred=true; }
            else x.starred=false;
          });
        }
        renderAll();
      });
    }
    return;
  }
  var sel=e.target.closest ? e.target.closest("[data-select]") : null;
  if(sel){
    e.stopPropagation();
    var cid=sel.getAttribute("data-select");
    state.selected[cid]=!state.selected[cid];
    renderLists(); renderReader(); return;
  }
  var act=e.target.closest ? e.target.closest("[data-act]") : null;
  if(act){
    e.stopPropagation();
    rowAction(act.getAttribute("data-act"), mailById(act.getAttribute("data-id")));
    return;
  }
  var open=e.target.closest ? e.target.closest("[data-open]") : null;
  if(open){
    openMail(open.getAttribute("data-open"));
  }
});
async function markAll(){
  var unreadThreads = activeMails().filter(function(m){ return m.unread; });
  if(!unreadThreads.length){ toast("Nothing unread"); return; }
  var ids = unreadThreads.map(function(m){ return threadKey(m); });
  var okIds = await threadBulkState(ids, { is_unread: false });
  if(!okIds.length){ toast("Couldn't sync that change with Gmail"); return; }
  okIds.forEach(function(tk){
    var m = rowByThreadKey(tk);
    if(m){ m.unread=false; m.unread_count=0; }
  });
  renderAll();
  toast(okIds.length===ids.length ? "All mail marked as read" : "Marked "+okIds.length+" of "+ids.length+" as read — the rest didn't sync");
}
$("#markAllBtn").addEventListener("click",markAll);

/* Search: local filter as you type, plus a debounced server search
   (GET /api/mails?q=) once the query is 2+ chars, so mail older than
   the loaded page is findable too. */
var searchTimer = null;
$("#searchInput").addEventListener("input",function(e){
  state.query=e.target.value;
  state.reading=false;
  if(state.view!=="inbox" && state.query) showView("inbox");
  var q = state.query.trim();
  if(searchTimer) clearTimeout(searchTimer);
  if(q.length >= 2){
    searchTimer = setTimeout(function(){
      /* Server search goes through the same paged loader (page 1);
         a failed search keeps the local matches and shows a visible
         error row with Retry instead of failing silently. */
      resetPager();
      fetchPage(null);
    }, 350);
  } else {
    if(SEARCH_RESULTS){
      /* Leaving an active server search reloads the inbox page 1. */
      SEARCH_RESULTS = null;
      resetPager();
      fetchPage(null);
    } else {
      SEARCH_RESULTS = null;
    }
  }
  renderLists(); renderReader();
});

/* ================= Compose =================
   Cc/Bcc, attachments (10 MB total cap), and server draft autosave
   (POST /api/drafts, then PUT) all live here. The draft is saved
   ~2s after typing stops and on close; an empty compose never creates
   a draft, and closing an empty draft deletes the server draft. */
function openCompose(){ $("#scrim").classList.add("open"); $("#composeModal").classList.add("open"); var to=$("#cTo"); if(to) to.focus(); }
function clearComposeFields(){
  ["cTo","cCc","cBcc","cSubject","cBody"].forEach(function(id){ var el=$("#"+id); if(el) el.value=""; });
  var cc=$("#cCc"), bcc=$("#cBcc");
  if(cc) cc.hidden = true;
  if(bcc) bcc.hidden = true;
  var tog=$("#cCcBccToggle"); if(tog) tog.setAttribute("aria-expanded", "false");
  var fn=$("#cForwardNote"); if(fn) fn.hidden = true;
  var ae=$("#cAttachError"); if(ae) ae.hidden = true;
}
function startFreshCompose(){
  currentDraftId = null;
  composeAttachments = [];
  forwardSkipped = [];
  if(draftTimer){ clearTimeout(draftTimer); draftTimer = null; }
  setDraftStatus("idle");
  clearComposeFields();
  renderAttachList("compose");
  openCompose();
}
function closeCompose(){
  /* Flush the autosave on close so nothing typed is lost; an empty
     compose deletes its server draft instead of littering Drafts. */
  saveDraft();
  if(draftTimer){ clearTimeout(draftTimer); draftTimer = null; }
  $("#scrim").classList.remove("open"); $("#composeModal").classList.remove("open");
}
$("#composeBtn").addEventListener("click",startFreshCompose);
$("#composeClose").addEventListener("click",closeCompose);
$("#scrim").addEventListener("click",function(){ closeCompose(); closeKeyModal(); });
document.addEventListener("keydown",function(e){
  if(e.key!=="Escape") return;
  closeCompose(); closeKeyModal();
  $("#sidebar").classList.remove("open"); $("#navScrim").classList.remove("open");
  closeMenus();
});
/* Cc / Bcc toggle (Gmail-style, collapsed by default). */
$("#cCcBccToggle").addEventListener("click",function(){
  var cc=$("#cCc"), bcc=$("#cBcc");
  var show = !cc || cc.hidden;
  if(cc) cc.hidden = !show;
  if(bcc) bcc.hidden = !show;
  this.setAttribute("aria-expanded", show ? "true" : "false");
  if(show && cc) cc.focus();
});
/* ---- Attachments (compose + reply share these) ---- */
function attachTotal(list){ return list.reduce(function(s,f){ return s + (f.size || 0); }, 0); }
function addAttachFiles(fileList, which){
  var list = (which === "reply") ? replyAttachments : composeAttachments;
  var errEl = (which === "reply") ? $("#rAttachError") : $("#cAttachError");
  var refused = [];
  for(var i=0;i<fileList.length;i++){
    var f = fileList[i];
    if(attachTotal(list) + (f.size || 0) > ATTACH_MAX_TOTAL) refused.push(f.name);
    else list.push(f);
  }
  if(refused.length && errEl){
    errEl.textContent = "Couldn't attach " + refused.join(", ") + " — the total would pass 10 MB (" + formatBytes(attachTotal(list)) + " attached). Remove a file to make room.";
    errEl.hidden = false;
  } else if(errEl){
    errEl.hidden = true;
  }
  renderAttachList(which);
}
function renderAttachList(which){
  var list = (which === "reply") ? replyAttachments : composeAttachments;
  var listEl = (which === "reply") ? $("#rAttachList") : $("#cAttachList");
  var totalEl = (which === "reply") ? $("#rAttachTotal") : $("#cAttachTotal");
  if(totalEl) totalEl.textContent = list.length ? list.length + " file" + (list.length > 1 ? "s" : "") + ", " + formatBytes(attachTotal(list)) + " of 10 MB" : "";
  if(!listEl) return;
  listEl.innerHTML = list.map(function(f, i){
    return '<div class="attach-item"><span class="att-name">' + escapeHtml(f.name) + '</span><span class="att-size">' + escapeHtml(formatBytes(f.size)) + '</span><button type="button" data-remove-att="' + i + '" data-att-list="' + which + '" aria-label="Remove attachment">Remove</button></div>';
  }).join("");
}
var cAttBtn=$("#cAttachBtn"), cAttInput=$("#cAttachInput");
if(cAttBtn && cAttInput){
  cAttBtn.addEventListener("click",function(){ cAttInput.click(); });
  cAttInput.addEventListener("change",function(){
    addAttachFiles(cAttInput.files, "compose");
    cAttInput.value = "";
    scheduleDraftAutosave();
  });
}
function showForwardNote(){
  var fn=$("#cForwardNote");
  if(!fn) return;
  if(!forwardSkipped.length){ fn.hidden = true; return; }
  fn.textContent = forwardSkipped.length + " attachment" + (forwardSkipped.length > 1 ? "s" : "") + " could not be re-attached: "
    + forwardSkipped.map(function(s){ return s.filename + " (" + s.reason + ")"; }).join(", ") + ".";
  fn.hidden = false;
}
/* ---- Draft autosave ---- */
function composeDraftPayload(){
  return {
    /* HOTFIX 2026-11-16: `to` was a plain string here while
       DraftCreateRequest expects a list — Pydantic 422'd every
       autosave, so the header always showed "Couldn't save draft —
       retry". cc/bcc already used parseAddressList. */
    to: parseAddressList($("#cTo") ? $("#cTo").value : ""),
    cc: parseAddressList($("#cCc") ? $("#cCc").value : ""),
    bcc: parseAddressList($("#cBcc") ? $("#cBcc").value : ""),
    subject: $("#cSubject") ? $("#cSubject").value.trim() : "",
    body: $("#cBody") ? $("#cBody").value : ""
  };
}
function composeIsEmpty(){
  var p = composeDraftPayload();
  return !p.to.length && !p.cc.length && !p.bcc.length && !p.subject && !p.body.trim() && !composeAttachments.length;
}
function setDraftStatus(state, when){
  draftSaveState = state;
  var el = $("#cDraftStatus");
  if(!el) return;
  el.classList.remove("error");
  if(state === "saving"){ el.hidden = false; el.textContent = "Saving…"; }
  else if(state === "saved"){ el.hidden = false; el.textContent = "Draft saved " + (when || formatClock(new Date())); }
  else if(state === "error"){ el.hidden = false; el.textContent = "Couldn't save draft — retry"; el.classList.add("error"); }
  else { el.hidden = true; el.textContent = ""; }
}
var cDraftStatusEl=$("#cDraftStatus");
if(cDraftStatusEl) cDraftStatusEl.addEventListener("click",function(){ if(draftSaveState === "error") saveDraft(); });
function deleteDraft(id){
  if(id == null) return Promise.resolve(false);
  return apiFetch("/api/drafts/" + encodeURIComponent(id), { method: "DELETE" }).then(function(res){ return res.ok; }).catch(function(){ return false; });
}
async function saveDraft(){
  if(draftSaveState === "saving") return false;
  /* An empty compose never creates a draft; closing one deletes the
     server draft instead of leaving an empty row in Drafts. */
  if(composeIsEmpty()){
    if(currentDraftId != null){
      var oldId = currentDraftId;
      currentDraftId = null;
      await deleteDraft(oldId);
    }
    setDraftStatus("idle");
    return true;
  }
  setDraftStatus("saving");
  try {
    var payload = composeDraftPayload();
    var res, d;
    if(currentDraftId != null){
      res = await apiFetch("/api/drafts/" + encodeURIComponent(currentDraftId), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      d = await res.json().catch(function(){ return {}; });
    } else {
      res = await apiFetch("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      d = await res.json().catch(function(){ return {}; });
      if(res.ok){
        var nid = (d.draft && d.draft.id != null) ? d.draft.id : (d.id != null ? d.id : d.draft_id);
        if(nid != null) currentDraftId = nid;
      }
    }
    if(!res.ok) throw new Error((d && d.detail) || "save failed");
    setDraftStatus("saved");
    renderFolderBadges();
    return true;
  } catch(e){
    setDraftStatus("error");
    return false;
  }
}
function scheduleDraftAutosave(){
  if(draftTimer) clearTimeout(draftTimer);
  draftTimer = setTimeout(function(){ draftTimer = null; saveDraft(); }, 2000);
}
["cTo","cCc","cBcc","cSubject","cBody"].forEach(function(id){
  var el = document.getElementById(id);
  if(el) el.addEventListener("input", scheduleDraftAutosave);
});
/* Reopen a server draft in compose, with cc/bcc/subject/body and any
   attachments note the draft carries. Files themselves cannot be
   restored from the server draft — the note says so plainly. */
function openDraft(row){
  var d = row._draft || {};
  startFreshCompose();
  currentDraftId = row.draft_id;
  var to=$("#cTo"), cc=$("#cCc"), bcc=$("#cBcc"), sub=$("#cSubject"), bod=$("#cBody");
  if(to) to.value = addressListToString(d.to) || d.to || "";
  var ccList = parseAddressList(d.cc), bccList = parseAddressList(d.bcc);
  if(cc && ccList.length){ cc.value = ccList.join(", "); cc.hidden = false; }
  if(bcc && bccList.length){ bcc.value = bccList.join(", "); bcc.hidden = false; }
  if(ccList.length || bccList.length){ var tog=$("#cCcBccToggle"); if(tog) tog.setAttribute("aria-expanded", "true"); }
  if(sub) sub.value = d.subject || "";
  if(bod) bod.value = d.body || "";
  var fn=$("#cForwardNote");
  var note = d.attachments_note || d.attachmentsNote || "";
  if(!note && Array.isArray(d.attachments) && d.attachments.length){
    note = "This draft had " + d.attachments.length + " attachment" + (d.attachments.length > 1 ? "s" : "") + " saved with it — re-attach the files before sending.";
  }
  if(fn && note){ fn.textContent = note; fn.hidden = false; }
  setDraftStatus("saved", formatClock(d.updated_at || d.created_at || new Date()));
}
var composeSending = false;
$("#composeForm").addEventListener("submit",async function(e){
  e.preventDefault();
  if(composeSending) return;
  var to=$("#cTo").value.trim(), sub=$("#cSubject").value.trim(), body=$("#cBody").value;
  if(!to){ toast("Add a recipient first"); return; }
  if(!sub){ toast("Add a subject first"); return; }
  if(!body.trim()){ toast("Write your message first"); return; }
  var ccList = parseAddressList($("#cCc") ? $("#cCc").value : "");
  var bccList = parseAddressList($("#cBcc") ? $("#cBcc").value : "");
  composeSending = true;
  try {
    var res;
    if(composeAttachments.length){
      /* Attachments ride as FormData (cc/bcc as repeated fields);
         JSON is used when there is nothing to upload. */
      var fd = new FormData();
      fd.append("to", to);
      fd.append("subject", sub);
      fd.append("body", body);
      ccList.forEach(function(a){ fd.append("cc", a); });
      bccList.forEach(function(a){ fd.append("bcc", a); });
      composeAttachments.forEach(function(f){ fd.append("attachments", f, f.name); });
      res = await apiFetch("/api/mails/send", { method: "POST", body: fd });
    } else {
      var payload = { to: to, subject: sub, body: body };
      if(ccList.length) payload.cc = ccList;
      if(bccList.length) payload.bcc = bccList;
      res = await apiFetch("/api/mails/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    }
    var d = await res.json().catch(function(){ return {}; });
    if(res.ok){
      /* The server may report attachments it could not re-attach on
         a forward (skipped_attachments); surface that with the send
         result instead of dropping it silently. */
      var skipped = d.skipped_attachments || d.skipped || [];
      var skippedCount = Array.isArray(skipped) ? skipped.length : (Number(skipped) || 0);
      if(currentDraftId != null){ var sentDraftId = currentDraftId; currentDraftId = null; await deleteDraft(sentDraftId); }
      composeAttachments = [];
      forwardSkipped = [];
      clearComposeFields();
      renderAttachList("compose");
      setDraftStatus("idle");
      closeCompose();
      if(skippedCount){
        var names = Array.isArray(skipped) ? skipped.map(function(s){ return (s && s.filename) || s; }).join(", ") : "";
        toast("Sent — " + skippedCount + " attachment" + (skippedCount > 1 ? "s" : "") + " could not be re-attached" + (names ? ": " + names : ""));
      } else {
        toast("Sent");
      }
      /* A sent draft leaves the Drafts folder; refresh it if open. */
      if(currentFolder() === "drafts"){ resetPager(); fetchPage(null); }
      else renderFolderBadges();
    } else {
      /* Keep the draft open so nothing is lost, and make sure the
         failed send is persisted as a server draft. */
      toast(d.detail || "Couldn't send the email");
      await saveDraft();
    }
  } catch(err){
    toast("Couldn't send the email — check connection");
    await saveDraft();
  } finally {
    composeSending = false;
  }
});
$("#copyDraft").addEventListener("click",function(){
  var txt="To: "+$("#cTo").value+"\nSubject: "+$("#cSubject").value+"\n\n"+$("#cBody").value;
  var ccV=$("#cCc") ? $("#cCc").value.trim() : "";
  var bccV=$("#cBcc") ? $("#cBcc").value.trim() : "";
  if(ccV) txt="To: "+$("#cTo").value+"\nCc: "+ccV+"\nSubject: "+$("#cSubject").value+"\n\n"+$("#cBody").value;
  if(bccV) txt="To: "+$("#cTo").value+(ccV?"\nCc: "+ccV:"")+"\nBcc: "+bccV+"\nSubject: "+$("#cSubject").value+"\n\n"+$("#cBody").value;
  if(navigator.clipboard) navigator.clipboard.writeText(txt);
  toast("Draft copied");
});

/* ================= AI Assistant ================= */
function addMsg(who,html){
  var t=$("#chatThread");
  var d=document.createElement("div");
  d.className="msg "+who;
  if(who==="ai"){
    d.innerHTML='<span class="who-ic"><img src="assets/logo-mark.png" alt="" onerror="this.src=\'assets/logo-mark.svg\'" /></span><div class="bubble">'+stripUnsafeHtml(html)+'</div>';
  } else {
    d.innerHTML='<div class="bubble">'+escapeHtml(html)+'</div>';
  }
  t.appendChild(d);
  d.scrollIntoView({behavior:"smooth",block:"end"});
}
function answerFor(q){
  var s=q.toLowerCase(), c=counts();
  if(s.indexOf("urgent")!==-1 || s.indexOf("deadline")!==-1){
    var u=MAILS.filter(function(m){return m.urgent;});
    var items=u.map(function(m){return "<b>"+escapeHtml(m.sender)+"</b> — "+escapeHtml(m.subject);}).join("<br>");
    return u.length ? "You have <b>"+c.urgent+" urgent mails</b>:<br>"+items : "There are no urgent mails in the current inbox.";
  }
  if(s.indexOf("summar")!==-1 || s.indexOf("today")!==-1){
    return "Here is today's brief: <b>"+c.today+" mails arrived</b>, "+c.unread+" are still unread, and <b>"+c.urgent+" are urgent</b>. <b>"+c.reply+" threads await your reply.</b>";
  }
  if(s.indexOf("reply")!==-1 || s.indexOf("waiting")!==-1){
    var r=MAILS.filter(function(m){return m.needsReply;});
    var names=r.map(function(m){return "<b>"+escapeHtml(m.sender)+"</b>";}).join(", ");
    return r.length ? "<b>"+c.reply+" threads are waiting for your reply</b>: "+names+"." : "No threads are waiting for your reply.";
  }
  if(s.indexOf("draft")!==-1){
    var top=MAILS.filter(function(m){return m.urgent;})[0];
    return top ? "Here is a draft for the most urgent mail — <b>"+escapeHtml(top.sender)+"</b> (“"+escapeHtml(top.subject)+"”):<br><br><i>"+escapeHtml(draftReply(top)).replace(/\n/g,"<br>")+"</i>" : "There are no urgent mails to draft a reply for.";
  }
  return "Your inbox currently has <b>"+c.unread+" unread</b> and <b>"+c.urgent+" urgent</b> mails.";
}
async function handleChat(v){
  if(!v) return;
  addMsg("me", v);
  
  // Create thinking bubble with live animation
  var thinkId = "think_" + Date.now();
  var t = $("#chatThread");
  var d = document.createElement("div");
  d.className = "msg ai";
  d.id = thinkId;
  d.innerHTML = '<span class="who-ic"><img src="assets/logo-mark.png" alt="" onerror="this.src=\'assets/logo-mark.svg\'" /></span><div class="bubble"><i>Thinking\u2026</i></div>';
  t.appendChild(d);
  d.scrollIntoView({behavior:"smooth",block:"end"});

  try {
    var res = await apiFetch("/api/chat-assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: v })
    });
    var data = await res.json();
    var th = document.getElementById(thinkId);
    if(th) th.remove();
    if(res.ok && data.answer){
      addMsg("ai", data.answer);
    } else {
      addMsg("ai", answerFor(v));
    }
  } catch(err){
    var th = document.getElementById(thinkId);
    if(th) th.remove();
    addMsg("ai", answerFor(v));
  }
}

$("#chatForm").addEventListener("submit",function(e){
  e.preventDefault();
  var v=$("#chatText").value.trim();
  if(!v) return;
  $("#chatText").value="";
  handleChat(v);
});

$$("[data-ask]").forEach(function(b){
  b.addEventListener("click",function(){
    var q=b.getAttribute("data-ask");
    handleChat(q);
  });
});

/* ================= Gmail inbox wiring ================= */
$$("#gmTabs .gm-tab").forEach(function(t){
  t.addEventListener("click",function(){ state.reading=false; setCategory(t.getAttribute("data-cat")); renderReader(); });
});
function closeMenus(){ $$(".gm-menu").forEach(function(mn){ mn.classList.remove("open"); }); }
$("#gmCaret").addEventListener("click",function(e){ e.stopPropagation(); var m=$("#gmSelectMenu"); var was=m.classList.contains("open"); closeMenus(); if(!was) m.classList.add("open"); });
$("#gmMore").addEventListener("click",function(e){ e.stopPropagation(); var m=$("#gmMoreMenu"); var was=m.classList.contains("open"); closeMenus(); if(!was) m.classList.add("open"); });
document.addEventListener("click",function(e){ if(!e.target.closest || !e.target.closest(".gm-menu")) closeMenus(); });
function selectWhere(fn){
  state.selected={};
  filteredMails().forEach(function(m){ if(fn(m)) state.selected[threadKey(m)]=true; });
  renderLists();
}
$$("#gmSelectMenu [data-selmenu]").forEach(function(b){
  b.addEventListener("click",function(){
    var k=b.getAttribute("data-selmenu"); closeMenus();
    if(k==="all") selectWhere(function(){ return true; });
    else if(k==="none"){ state.selected={}; renderLists(); }
    else if(k==="read") selectWhere(function(m){ return !m.unread; });
    else if(k==="unread") selectWhere(function(m){ return m.unread; });
    else if(k==="starred") selectWhere(function(m){ return m.starred; });
    else if(k==="unstarred") selectWhere(function(m){ return !m.starred; });
  });
});
$$("#gmMoreMenu [data-moremenu]").forEach(function(b){
  b.addEventListener("click",function(){
    var k=b.getAttribute("data-moremenu"); closeMenus();
    if(k==="markall") markAll();
    else if(k==="selectall") selectWhere(function(){ return true; });
  });
});
$("#gmMaster").addEventListener("click",function(){
  var list=filteredMails();
  var allSel=list.length>0 && list.every(function(m){ return state.selected[threadKey(m)]; });
  if(allSel){ list.forEach(function(m){ delete state.selected[threadKey(m)]; }); }
  else { list.forEach(function(m){ state.selected[threadKey(m)]=true; }); }
  renderLists();
});
$("#gmMaster2").addEventListener("click",function(){ state.selected={}; renderLists(); });

$("#gmFilterClear").addEventListener("click",function(){
  state.query=""; state.selected={};
  SEARCH_RESULTS = null;
  var si=$("#searchInput"); if(si) si.value="";
  setFilter("all");
});
/* ================= Labels =================
   The label button used to toast "Labels are shown on each
   conversation" and do nothing. It now opens a real menu of the
   user's Gmail labels; picking one applies (or removes, if already
   on) it on every selected conversation via POST /api/mails/{id}/labels. */
async function loadLabels(){
  if(LABELS_CACHE) return LABELS_CACHE;
  try {
    var res = await apiFetch("/api/labels");
    var d = await res.json();
    if(res.ok && d.status === "success"){
      LABELS_CACHE = d.labels || [];
      return LABELS_CACHE;
    }
    return null;
  } catch(e){
    /* A failed fetch is not an empty label list — callers must say so. */
    return null;
  }
}
function mailLabelIds(m){
  return (m.labels || "").split(",").map(function(s){ return s.trim(); }).filter(Boolean);
}
async function openLabelMenu(anchorBtn, ids){
  var menu = $("#gmLabelMenu");
  if(!menu) return;
  var labels = await loadLabels();
  closeMenus();
  if(labels === null){
    toast("Couldn't load labels — try again");
    return;
  }
  if(!labels.length){
    toast("No labels yet — create one in Gmail first");
    return;
  }
  var targetIds = (ids && ids.length) ? ids : (currentMail() ? [currentMail().id] : []);
  if(!targetIds.length){ toast("Open a conversation first"); return; }
  menu.innerHTML = labels.map(function(l){
    var on = targetIds.some(function(id){ var m = mailById(id); return m && mailLabelIds(m).indexOf(l.id) !== -1; });
    return '<button data-label-id="' + escapeHtml(l.id) + '" data-label-on="' + (on ? "1" : "0") + '">' + (on ? "✓ " : "") + escapeHtml(l.name) + '</button>';
  }).join("");
  /* The menu lives in the list toolbar, which is hidden while reading —
     so it is moved to <body> and positioned at the button instead. */
  if(menu.parentNode !== document.body) document.body.appendChild(menu);
  var r = anchorBtn.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.top = (r.bottom + 4) + "px";
  menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 210)) + "px";
  menu.style.right = "auto";
  menu.classList.add("open");
  menu.querySelectorAll("[data-label-id]").forEach(function(btn){
    btn.addEventListener("click", function(){
      var lid = btn.getAttribute("data-label-id");
      var isOn = btn.getAttribute("data-label-on") === "1";
      closeMenus();
      applyLabelToMails(targetIds, lid, !isOn);
    });
  });
}
async function applyLabelToMails(ids, labelId, add){
  var done = 0;
  for(var i=0;i<ids.length;i++){
    /* Labels are a single-message endpoint: apply to the thread row's
       message id (the row's .id), not the thread id. */
    var row = mailById(ids[i]);
    var mid = (row && row.id != null) ? row.id : ids[i];
    try {
      var res = await apiFetch("/api/mails/" + mid + "/labels", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(add ? { add: [labelId] } : { remove: [labelId] })
      });
      if(res.ok){
        done++;
        var m = mailById(ids[i]);
        if(m){
          var cur = mailLabelIds(m);
          if(add && cur.indexOf(labelId)===-1) cur.push(labelId);
          if(!add) cur = cur.filter(function(x){ return x !== labelId; });
          m.labels = cur.join(",");
        }
      }
    } catch(e){}
  }
  state.selected = {};
  renderAll();
  toast(done ? (add ? "Label applied" : "Label removed") : "Couldn't change the label");
}
$$("#gmBarSel [data-selact]").forEach(function(b){
  b.addEventListener("click",function(){
    var act=b.getAttribute("data-selact");
    var ids=selectedIds();
    if(!ids.length) return;
    if(act==="snooze"){ toast("Snooze isn\u2019t available yet \u2014 these mails stay in your inbox"); state.selected={}; renderAll(); return; }
    if(act==="label"){ openLabelMenu(b, ids); return; }
    /* Drafts folder: Delete removes the server drafts, one call each. */
    if(currentFolder() === "drafts" && act === "trash"){
      var drafts = ids.map(function(tk){ return rowByThreadKey(tk); }).filter(function(m){ return m && m.draft_id; });
      var done = 0;
      (async function(){
        for(var di=0; di<drafts.length; di++){
          try {
            var r = await apiFetch("/api/drafts/" + encodeURIComponent(drafts[di].draft_id), { method: "DELETE" });
            if(r.ok){ done++; var i = MAILS.indexOf(drafts[di]); if(i > -1) MAILS.splice(i, 1); }
          } catch(e){}
        }
        state.selected = {};
        renderAll();
        toast(done ? ("Deleted " + done + " draft" + (done > 1 ? "s" : "")) : "Couldn't delete those drafts");
      })();
      return;
    }
    if(act==="restore"){
      var folder = currentFolder();
      var rpayload = folder === "trash" ? { is_trash: false } : folder === "spam" ? { is_spam: false } : { is_archived: false };
      threadBulkState(ids, rpayload).then(function(okIds){
        if(!okIds.length){ toast("Couldn't sync that change with Gmail"); return; }
        okIds.forEach(function(tk){
          var m=rowByThreadKey(tk); if(!m) return;
          var i = MAILS.indexOf(m); if(i > -1) MAILS.splice(i, 1);
        });
        state.selected={};
        renderAll();
        toast(okIds.length===ids.length ? "Conversations restored to inbox" : "Restored "+okIds.length+" of "+ids.length+" — Gmail didn't accept the rest");
      });
      return;
    }
    var payload = act==="archive" ? { is_archived: true } : act==="trash" ? { is_trash: true } : act==="spam" ? { is_spam: true } : { is_unread: false };
    /* Thread state is applied per thread (no bulk thread endpoint);
       local state flips only for the threads Gmail accepted. */
    threadBulkState(ids, payload).then(function(okIds){
      if(!okIds.length){ toast("Couldn't sync that change with Gmail"); return; }
      okIds.forEach(function(tk){
        var m=rowByThreadKey(tk); if(!m) return;
        if(act==="archive" || act==="spam") m.archived=true;
        else if(act==="trash") m.trashed=true;
        else if(act==="read"){ m.unread=false; m.unread_count=0; }
      });
      state.selected={};
      renderAll();
      if(okIds.length===ids.length) toast(act==="archive" ? "Conversations archived" : act==="read" ? "Marked as read" : act==="spam" ? "Reported as spam" : "Conversations deleted");
      else toast("Updated "+okIds.length+" of "+ids.length+" — Gmail didn't accept the rest");
    });
  });
});
/* Reading toolbar */
$("#gmBack").addEventListener("click",function(){ state.reading=false; renderAll(); });
function currentAction(act){ rowAction(act, currentMail()); }
$("#gmReadArchive").addEventListener("click",function(){ currentAction("archive"); });
$("#gmReadRestore").addEventListener("click",function(){ currentAction("restore"); });
$("#gmReadTrash").addEventListener("click",function(){ currentAction("trash"); });
$("#gmReadSpam").addEventListener("click",function(){ currentAction("spam"); });
$("#gmReadSnooze").addEventListener("click",function(){ toast("Snooze isn\u2019t available yet \u2014 this mail stays in your inbox"); state.reading=false; renderAll(); });
$("#gmReadUnread").addEventListener("click",function(){
  var m=currentMail(); if(!m) return;
  var tk=threadKey(m);
  threadStateToBackend(tk, { is_unread: true }).then(function(ok){
    if(!ok) return;
    m.unread=true;
    m.unread_count=Math.max(1, m.unread_count || 0);
    state.reading=false;
    renderAll();
  });
});
$("#gmReadLabel").addEventListener("click",function(){
  var m=currentMail(); if(m) openLabelMenu(this, [m.id]);
});
function stepMail(dir){
  var list=filteredMails();
  var m=currentMail(); if(!m) return;
  var idx=-1;
  for(var i=0;i<list.length;i++){ if(String(threadKey(list[i]))===String(threadKey(m))){ idx=i; break; } }
  var next=list[idx+dir];
  if(next) openMail(threadKey(next));
}
$("#gmPrev").addEventListener("click",function(){ stepMail(-1); });
$("#gmNext").addEventListener("click",function(){ stepMail(1); });
/* Real pager: Older/Newer walk the cursor stack (see goOlder/goNewer). */
var gmOlderBtn=$("#gmOlder"), gmNewerBtn=$("#gmNewer");
if(gmOlderBtn) gmOlderBtn.addEventListener("click",goOlder);
if(gmNewerBtn) gmNewerBtn.addEventListener("click",goNewer);

/* ================= Live Backend Integration ================= */
async function initBackend(){
  var dchip = $("#currentDateChip");
  if(dchip){
    dchip.textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  }

  try {
    var res = await apiFetch("/api/dashboard");
    var data = await res.json();
    /* Global inbox counts for the dashboard stats, when the backend
       provides them (renderStats prefers these over local counts). */
    if(data.inbox_counts) INBOX_COUNTS = data.inbox_counts;
    else if(data.data && data.data.inbox_counts) INBOX_COUNTS = data.data.inbox_counts;
    /* Per-folder counts for the sidebar badges, when the backend
       provides them (also accepted nested under inbox_counts). */
    var fc = data.folder_counts || (data.data && data.data.folder_counts)
      || (INBOX_COUNTS && INBOX_COUNTS.folder_counts);
    if(fc) FOLDER_COUNTS = fc;
    /* HOTFIX 2026-11-16: paint the stats/badges from the counts just
       fetched, NOW — previously renderStats only ran after the first
       inbox page loaded, so with the Primary fetch broken the
       dashboard kept the boot-time zeros even though the counts had
       arrived. */
    AI_BRIEF_FROM_REPORT = false;
    renderAll();
    if(data.status === "success" || data.status === "warning"){
      var cfg = data.config || {};
      if(cfg.model){
        var mt = $("#topModelTag");
        if(mt) mt.textContent = cfg.model;
      }
      if(cfg.recipient_email){
        var em = $("#userEmail");
        if(em) em.textContent = cfg.recipient_email;
        var un = $("#userName");
        var namePart = cfg.recipient_email.split("@")[0];
        if(un) un.textContent = namePart.charAt(0).toUpperCase() + namePart.slice(1);
        var av = $("#userAvatar");
        if(av) av.textContent = initials(namePart);
        var gr = $("#greetingTitle");
        if(gr){
          var hr = new Date().getHours();
          var greet = hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening";
          gr.textContent = greet + ", " + (namePart.charAt(0).toUpperCase() + namePart.slice(1));
        }
      }

      var latest = data.data && data.data.latest;
      if(latest && latest.summary_markdown){
        var briefEl = $("#aiBriefText");
        if(briefEl){
          var briefTxt = String(latest.summary_markdown).slice(0, 320) + (String(latest.summary_markdown).length > 320 ? "\u2026" : "");
          briefEl.innerHTML = "<b>Today's Briefing:</b> " + escapeHtml(briefTxt);
          /* Mark the briefing as the brief panel's content so the
             next renderStats() does not overwrite it with the counts
             sentence (HOTFIX 2026-11-16). */
          AI_BRIEF_FROM_REPORT = true;
        }
        if(latest.action_items){
          var items = null;
          try { items = typeof latest.action_items === "string" ? JSON.parse(latest.action_items) : latest.action_items; }
          catch(parseErr){ console.log("Action items parse note:", parseErr); }
          if(items && items.length){
            var pts = items.slice(0, 4).map(function(it){
              return "<li><span class='dot'></span><span><b>" + escapeHtml(it.sender || "Action") + ":</b> " + escapeHtml(it.task || "") + " (" + escapeHtml(it.due || "ASAP") + ")</span></li>";
            });
            $("#aiBriefPoints").innerHTML = pts.join("");
          }
        }
      }
    }
  } catch(e){
    console.log("Initial dashboard load:", e);
  }

  // Load page 1 of the inbox (folder=inbox, cat=primary, limit=30).
  // fetchPage shows a visible error with Retry in the inbox list if
  // this fails, instead of leaving an empty list.
  resetPager();
  await fetchPage(null);
}

var syncBtn = $("#syncLiveBtn");
if(syncBtn){
  syncBtn.addEventListener("click", async function(){
    syncBtn.disabled = true;
    toast("Syncing with Gmail...");
    try {
      var res = await apiFetch("/api/sync-now", { method: "POST" });
      var data = await res.json();
      if(res.ok){
        toast(data.message || ("Processed " + (data.processed_count || 0) + " emails successfully!"));
        await initBackend();
      } else {
        toast(data.detail || "Sync failed. Check credentials.");
      }
    } catch(e){
      toast("Sync failed: Network or server error");
    } finally {
      syncBtn.disabled = false;
    }
  });
}

$("#gmRefresh").addEventListener("click", async function(){
  toast("Synchronizing inbox with Gmail...");
  try {
    var syncRes = await apiFetch("/api/sync-inbox-state", { method: "POST" });
    if(!syncRes.ok) throw new Error("sync failed");
    await initBackend();
    toast("Inbox synchronized");
  } catch(e){
    toast("Sync failed — showing the last loaded inbox");
  }
});

/* ================= Config View Logic ================= */
async function loadConfig(){
  try {
    var res = await apiFetch("/api/config");
    var data = await res.json();
    if(data.status === "success"){
      var s = data.schedule || {};
      var r = data.range || {};

      var enEl = $("#cfgScheduleEnabled");
      if(enEl) enEl.checked = !!s.enabled;

      var tmEl = $("#cfgScheduleTime");
      if(tmEl && s.time) tmEl.value = s.time;

      var tzEl = $("#cfgScheduleTz");
      if(tzEl && s.timezone) tzEl.value = s.timezone;

      var prEl = $("#cfgRangePreset");
      if(prEl && r.preset) prEl.value = r.preset;

      var cdf = $("#customDateFields");
      if(cdf) cdf.style.display = (r.preset === "custom") ? "block" : "none";

      var csEl = $("#cfgCustomStart");
      if(csEl && r.custom_start) csEl.value = r.custom_start;

      var ceEl = $("#cfgCustomEnd");
      if(ceEl && r.custom_end) ceEl.value = r.custom_end;

      var whBox = $("#cronWebhookBox");
      if(whBox) whBox.textContent = "POST " + window.location.origin + "/api/cron-trigger";
    }
  } catch(e){
    console.log("Config load failed:", e);
  }
}

var prEl = $("#cfgRangePreset");
if(prEl){
  prEl.addEventListener("change", function(){
    var cdf = $("#customDateFields");
    if(cdf) cdf.style.display = (this.value === "custom") ? "block" : "none";
  });
}

var schForm = $("#scheduleForm");
if(schForm){
  schForm.addEventListener("submit", async function(e){
    e.preventDefault();
    var payload = {
      schedule: {
        enabled: $("#cfgScheduleEnabled").checked,
        time: $("#cfgScheduleTime").value,
        timezone: $("#cfgScheduleTz").value
      }
    };
    try {
      var res = await apiFetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if(res.ok){
        toast("Summary schedule updated");
      } else {
        toast("Failed to update schedule");
      }
    } catch(err){
      toast("Error updating schedule");
    }
  });
}

var rngForm = $("#rangeForm");
if(rngForm){
  rngForm.addEventListener("submit", async function(e){
    e.preventDefault();
    var payload = {
      range: {
        preset: $("#cfgRangePreset").value,
        custom_start: $("#cfgCustomStart") ? $("#cfgCustomStart").value : "",
        custom_end: $("#cfgCustomEnd") ? $("#cfgCustomEnd").value : ""
      }
    };
    try {
      var res = await apiFetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if(res.ok){
        toast("Date range preference saved");
      } else {
        toast("Failed to save date range");
      }
    } catch(err){
      toast("Error saving date range");
    }
  });
}

var triggerBtn = $("#triggerSummaryNowBtn");
if(triggerBtn){
  triggerBtn.addEventListener("click", async function(){
    triggerBtn.disabled = true;
    toast("Running summary...");
    try {
      var res = await apiFetch("/api/summary/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force_all: true })
      });
      var data = await res.json();
      if(res.ok && data.status === "success"){
        toast("Summary completed! Analyzed " + data.total_emails + " emails across " + data.total_batches + " batches.");
        await initBackend();
        loadSummaryJobs();
      } else {
        toast(data.message || data.detail || "Summary execution failed");
      }
    } catch(e){
      toast("Summary execution failed: Network error");
    } finally {
      triggerBtn.disabled = false;
    }
  });
}

async function loadSummaryJobs(){
  var tb = $("#summaryJobsTableBody");
  if(!tb) return;
  try {
    var res = await apiFetch("/api/summary/status");
    var data = await res.json();
    if(data.status === "success" && data.jobs && data.jobs.length){
      var rows = data.jobs.map(function(j){
        var dt = j.created_at ? new Date(j.created_at).toLocaleString() : "Recently";
        var statusBadge = j.status === "COMPLETED" ? '<span class="badge-tag badge-active">COMPLETED</span>' :
                          j.status === "PROCESSING" ? '<span class="badge-tag badge-cooldown">PROCESSING</span>' :
                          '<span class="badge-tag badge-disabled">' + escapeHtml(j.status) + '</span>';
        var urgentCount = j.urgent_count || 0;
        return "<tr>"
          + "<td><b>" + dt + "</b></td>"
          + "<td>" + statusBadge + "</td>"
          + "<td>" + escapeHtml(j.date_range_info || "last_1_day") + "</td>"
          + "<td>" + (j.total_emails || 0) + "</td>"
          + "<td>" + (j.completed_batches || 0) + " / " + (j.total_batches || 0) + "</td>"
          + "<td>" + (urgentCount > 0 ? '<b style="color:var(--red)">' + urgentCount + ' urgent</b>' : '0') + "</td>"
          + "</tr>";
      });
      tb.innerHTML = rows.join("");
    } else {
      tb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:20px">No automated summary executions logged yet.</td></tr>';
    }
  } catch(e){
    console.log("Error loading summary jobs:", e);
    tb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:20px">Couldn\u2019t load job history. Try Refresh.</td></tr>';
  }
}

var refJobsBtn = $("#refreshJobsBtn");
if(refJobsBtn){
  refJobsBtn.addEventListener("click", function(){
    loadSummaryJobs();
    toast("Jobs refreshed");
  });
}

/* ================= API Keys View Logic ================= */
async function loadApiKeys(){
  var tb = $("#apiKeysTableBody");
  if(!tb) return;
  try {
    var res = await apiFetch("/api/keys");
    var data = await res.json();
    if(data.status === "success" && data.keys && data.keys.length){
      var rows = data.keys.map(function(k){
        var provBadge = k.provider === "gemini" ? '<span class="badge-tag badge-gemini">Gemini</span>' :
                        k.provider === "groq" ? '<span class="badge-tag badge-groq">Groq</span>' :
                        '<span class="badge-tag badge-openai">OpenAI</span>';
        var statusBadge = !k.is_active ? '<span class="badge-tag badge-disabled">Disabled</span>' :
                          k.is_in_cooldown ? '<span class="badge-tag badge-cooldown">Cooldown</span>' :
                          '<span class="badge-tag badge-active">Active</span>';
        return "<tr>"
          + "<td>" + provBadge + "</td>"
          + "<td><b>" + escapeHtml(k.label) + "</b></td>"
          + "<td><code>" + escapeHtml(k.api_key_masked) + "</code></td>"
          + "<td>" + escapeHtml(k.model_name) + "</td>"
          + "<td>P" + k.priority + "</td>"
          + "<td>" + statusBadge + "</td>"
          + "<td>" + (k.success_requests || 0) + " / " + (k.failed_requests || 0) + "</td>"
          + "<td>"
          +   "<div style='display:flex;gap:6px'>"
          +     "<button class='btn' data-testkey='" + k.id + "' style='min-height:28px;padding:2px 8px;font-size:12px'>Test</button>"
          +     "<button class='btn' data-togglekey='" + k.id + "' data-active='" + (k.is_active ? "1" : "0") + "' style='min-height:28px;padding:2px 8px;font-size:12px'>" + (k.is_active ? "Disable" : "Enable") + "</button>"
          +     (k.id > 1 ? "<button class='btn' data-delkey='" + k.id + "' style='min-height:28px;padding:2px 8px;font-size:12px;color:var(--red)'>Delete</button>" : "")
          +   "</div>"
          + "</td>"
          + "</tr>";
      });
      tb.innerHTML = rows.join("");
    } else {
      tb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-3);padding:20px">No API keys found. Click "Add API Key" to add one.</td></tr>';
    }
  } catch(e){
    console.log("Error loading API keys:", e);
    tb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-3);padding:20px">Couldn\u2019t load API keys. Check the connection and try again.</td></tr>';
  }
}

// Modal handling
var addKeyModal = $("#apiKeyModal");
var openKeyModalBtn = $("#openAddKeyModalBtn");
var closeKeyModalBtn = $("#apiKeyModalClose");
var cancelKeyModalBtn = $("#apiKeyModalCancel");

if(openKeyModalBtn && addKeyModal){
  openKeyModalBtn.addEventListener("click", function(){
    $("#scrim").classList.add("open");
    addKeyModal.classList.add("open");
  });
}
function closeKeyModal(){
  $("#scrim").classList.remove("open");
  if(addKeyModal) addKeyModal.classList.remove("open");
}
if(closeKeyModalBtn) closeKeyModalBtn.addEventListener("click", closeKeyModal);
if(cancelKeyModalBtn) cancelKeyModalBtn.addEventListener("click", closeKeyModal);

// Auto-fill default model on provider change per rule 1.3 — only the
// two rule-sanctioned defaults (gemini-3.5-flash, openai/gpt-oss-120b).
// OpenAI has no sanctioned default: the field is left as the user typed
// it, and the backend requires an explicit model for generic providers
// instead of silently inventing one.
var provSelect = $("#keyProvider");
if(provSelect){
  provSelect.addEventListener("change", function(){
    var km = $("#keyModel");
    if(km){
      if(this.value === "gemini") km.value = "gemini-3.5-flash";
      else if(this.value === "groq") km.value = "openai/gpt-oss-120b";
    }
  });
}

// Add API Key form
var addKeyForm = $("#addApiKeyForm");
if(addKeyForm){
  addKeyForm.addEventListener("submit", async function(e){
    e.preventDefault();
    var payload = {
      provider: $("#keyProvider").value,
      label: $("#keyLabel").value.trim(),
      api_key: $("#keySecret").value.trim(),
      model_name: $("#keyModel").value.trim(),
      priority: parseInt($("#keyPriority").value, 10) || 1,
      rate_limit_rpm: 15
    };
    try {
      var res = await apiFetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      var data = await res.json();
      if(res.ok){
        toast("API Key added successfully!");
        closeKeyModal();
        addKeyForm.reset();
        loadApiKeys();
      } else {
        toast(data.detail || "Failed to add API key.");
      }
    } catch(err){
      toast("Error creating API key");
    }
  });
}

// Delegated key table clicks
document.addEventListener("click", async function(e){
  var testBtn = e.target.closest ? e.target.closest("[data-testkey]") : null;
  if(testBtn){
    var kid = testBtn.getAttribute("data-testkey");
    testBtn.disabled = true;
    toast("Testing key connection...");
    try {
      var res = await apiFetch("/api/keys/" + kid + "/test", { method: "POST" });
      var data = await res.json();
      if(res.ok){
        toast("Key verified successfully!");
        loadApiKeys();
      } else {
        toast(data.detail || "Key test failed.");
      }
    } catch(err){
      toast("Key test network error");
    } finally {
      testBtn.disabled = false;
    }
    return;
  }

  var toggleBtn = e.target.closest ? e.target.closest("[data-togglekey]") : null;
  if(toggleBtn){
    var kid = toggleBtn.getAttribute("data-togglekey");
    var isAct = toggleBtn.getAttribute("data-active") === "1";
    try {
      var res = await apiFetch("/api/keys/" + kid, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !isAct })
      });
      if(res.ok){
        toast(isAct ? "Key disabled" : "Key enabled");
        loadApiKeys();
      }
    } catch(err){
      toast("Error updating key status");
    }
    return;
  }

  var delBtn = e.target.closest ? e.target.closest("[data-delkey]") : null;
  if(delBtn){
    var kid = delBtn.getAttribute("data-delkey");
    if(confirm("Are you sure you want to delete this API key?")){
      try {
        var res = await apiFetch("/api/keys/" + kid, { method: "DELETE" });
        if(res.ok){
          toast("Key deleted");
          loadApiKeys();
        }
      } catch(err){
        toast("Error deleting key");
      }
    }
    return;
  }
});

/* ================= Admin token form ================= */
var atForm = $("#adminTokenForm");
if(atForm) atForm.addEventListener("submit", function(e){
  e.preventDefault();
  var v = $("#adminTokenInput").value.trim();
  if(!v){ toast("Paste the admin token first"); return; }
  setAdminToken(v);
  $("#adminTokenInput").value = "";
  refreshTokenStateLabel();
  hideAuthNotice();
  toast("Token saved for this tab — reloading data");
  initBackend();
});
var atClear = $("#adminTokenClear");
if(atClear) atClear.addEventListener("click", function(){ setAdminToken(""); refreshTokenStateLabel(); toast("Token cleared for this tab"); });
var atGo = $("#authNoticeGo");
if(atGo) atGo.addEventListener("click", function(){ showView("config"); var inp=$("#adminTokenInput"); if(inp) inp.focus(); });
refreshTokenStateLabel();

/* ================= Init ================= */
applyLocalState();
renderAll();
initBackend();
