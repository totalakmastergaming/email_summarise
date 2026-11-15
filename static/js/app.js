/* ================= Data ================= */
var MAILS = [];
/* Server search results (null = not searching; MAILS is the pool). */
var SEARCH_RESULTS = null;
/* User-created Gmail labels, loaded once per session. */
var LABELS_CACHE = null;

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
  return '<div class="mail-row'+(m.unread?" unread":"")+(state.selectedId===m.id?" selected":"")+'" data-open="'+m.id+'" role="button" tabindex="0">'
    + (m.unread?'<span class="unread-dot"></span>':'<span style="width:8px;flex:none"></span>')
    + '<span class="avatar" style="background:'+avatarColor(m.sender)+'">'+initials(m.sender)+'</span>'
    + '<span class="mail-main"><span class="mail-line1"><span class="mail-sender">'+escapeHtml(m.sender)+'</span>'+tagHtml(m)+'<span class="mail-time num">'+escapeHtml(m.time)+'</span></span>'
    + '<span class="mail-subject" style="display:block">'+escapeHtml(m.subject)+'</span>'
    + '<span class="mail-snip" style="display:block">'+escapeHtml(m.snippet)+'</span></span>'
    + '<button class="star-btn'+(m.starred?" on":"")+'" data-star="'+m.id+'" aria-label="Star this mail">'+starSvg()+'</button></div>';
}
function miniRowHtml(m){
  return '<button class="mini-mail" data-open="'+m.id+'"><span class="avatar" style="background:'+avatarColor(m.sender)+';width:34px;height:34px;font-size:12px">'+escapeHtml(initials(m.sender))+'</span>'
    + '<span class="mini-main"><span class="mini-row"><span class="mini-sender">'+escapeHtml(m.sender)+'</span><span class="mini-time num">'+escapeHtml(m.time)+'</span></span>'
    + '<span class="mini-subject" style="display:block">'+escapeHtml(m.subject)+'</span>'
    + '<span class="mini-snip" style="display:block">'+escapeHtml(m.snippet)+'</span></span></button>';
}

/* ================= Rendering ================= */
function renderStats(){
  var c=counts();
  $("#statUnread").textContent=c.unread;
  $("#statToday").textContent=c.today;
  $("#statUrgent").textContent=c.urgent;
  $("#statReply").textContent=c.reply;
  $("#navUnread").textContent=c.unread;
  $("#navUrgent").textContent=c.urgent;
  $("#statUnreadSub").innerHTML = c.unread>0 ? "Across "+activeMails().length+" conversations in your inbox" : "Inbox zero — nothing unread";
  $("#statTodaySub").innerHTML = "Since 12:00 AM today · <b>"+activeMails().filter(function(m){return m.today&&m.unread;}).length+" still unread</b>";
  var firstUrgent = activeMails().filter(function(m){return m.urgent;})[0];
  var firstReply = activeMails().filter(function(m){return m.needsReply;})[0];
  $("#statUrgentSub").innerHTML = firstUrgent ? "Priority: <b>"+escapeHtml(firstUrgent.subject)+"</b>" : "Nothing marked urgent";
  $("#statReplySub").innerHTML = firstReply ? "Waiting from: <b>"+escapeHtml(firstReply.sender)+"</b>" : "No one is waiting on you";
  /* AI brief */
  $("#aiBriefText").innerHTML = "You received <b>"+c.today+" mails today</b>. <b>"+c.urgent+" are urgent</b> and <b>"+c.reply+" threads are waiting for your reply.</b>";
  var pts=[];
  activeMails().filter(function(m){return m.urgent;}).slice(0,3).forEach(function(m){
    pts.push("<li><span class='dot'></span><span><b>"+escapeHtml(m.sender)+":</b> "+escapeHtml((m.summary||"").split(".")[0])+".</span></li>");
  });
  var extra = activeMails().filter(function(m){return m.needsReply && !m.urgent;});
  if(extra.length) pts.push("<li><span class='dot'></span><span>Also awaiting a reply: <b>"+extra.map(function(m){return escapeHtml(m.sender);}).join(", ")+"</b>.</span></li>");
  $("#aiBriefPoints").innerHTML = pts.join("");
}
function filteredMails(){
  var q=state.query.trim().toLowerCase();
  /* While searching, the pool is the server result set (SEARCH_RESULTS);
     otherwise it is the loaded inbox. Stats always use MAILS. */
  var pool = (q && SEARCH_RESULTS)
    ? SEARCH_RESULTS.filter(function(m){ return !m.trashed && !m.archived; })
    : activeMails();
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
  forward:'<svg viewBox="0 0 24 24"><path d="M14 9V5l7 7-7 7v-4.1c-5 0-8.5 1.6-11 5.1 1-5 4-10 11-11z"/></svg>'
};
function gmRowHtml(m){
  var sel = !!state.selected[m.id];
  return '<div class="gm-row'+(m.unread?" unread":"")+(sel?" checked":"")+'" data-open="'+m.id+'" role="button" tabindex="0">'
    + '<button class="gm-check sm'+(sel?" on":"")+'" data-select="'+m.id+'" aria-label="Select conversation"><span class="box">'+ICO.check+'</span></button>'
    + '<button class="gm-star'+(m.starred?" on":"")+'" data-star="'+m.id+'" aria-label="Star this mail">'+(m.starred?ICO.starF:ICO.starO)+'</button>'
    + '<span class="gm-avatar" style="background:'+avatarColor(m.sender)+'">'+initials(m.sender)+'</span>'
    + '<span class="gm-sender">'+escapeHtml(m.sender)+'</span>'
    + '<span class="gm-mid"><span class="gm-subj">'+escapeHtml(m.subject)+'</span><span class="gm-snip"> — '+escapeHtml(m.snippet)+'</span></span>'
    + '<span class="gm-labelchip">'+escapeHtml(m.tag)+'</span>'
    + '<span class="gm-date num">'+escapeHtml(m.time)+'</span>'
    + '<span class="gm-actions">'
    + '<button class="gm-tbtn sm" data-act="archive" data-id="'+m.id+'" aria-label="Archive">'+ICO.archive+'</button>'
    + '<button class="gm-tbtn sm" data-act="trash" data-id="'+m.id+'" aria-label="Delete">'+ICO.trash+'</button>'
    + '<button class="gm-tbtn sm" data-act="read" data-id="'+m.id+'" aria-label="Mark as '+(m.unread?"read":"unread")+'">'+(m.unread?ICO.mailOpen:ICO.mailClosed)+'</button>'
    + '<button class="gm-tbtn sm" data-act="snooze" data-id="'+m.id+'" aria-label="Snooze">'+ICO.snooze+'</button>'
    + '</span></div>';
}
function selectedIds(){
  return Object.keys(state.selected).filter(function(k){ return state.selected[k]; }).map(function(k){ return parseInt(k,10); });
}
function renderGmailChrome(){
  var list = filteredMails();
  var rangeTxt = list.length ? "1–"+list.length+" of "+list.length : "0 of 0";
  var r1=$("#gmRange"), r2=$("#gmRange2");
  if(r1) r1.textContent = rangeTxt;
  if(r2) r2.textContent = rangeTxt;
  var fc=$("#gmFootCount");
  if(fc) fc.textContent = list.length ? list.length+" conversation"+(list.length>1?"s":"")+" in this view" : "No conversations in this view";
  /* Tab "new" badges = unread in that category */
  var soc = activeMails().filter(function(m){ return m.cat==="social" && m.unread; }).length;
  var pro = activeMails().filter(function(m){ return m.cat==="promotions" && m.unread; }).length;
  var bs=$("#gmNewSocial"), bp=$("#gmNewPromo");
  if(bs){ bs.hidden = soc===0; bs.textContent = soc+" new"; }
  if(bp){ bp.hidden = pro===0; bp.textContent = pro+" new"; }
  $$("#gmTabs .gm-tab").forEach(function(t){
    var on = state.filter==="all" && !state.query && t.getAttribute("data-cat")===state.cat;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  /* Master checkbox + toolbar swap */
  var visIds = list.map(function(m){ return m.id; });
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
  if(fb){ fb.hidden = !label; if(label) ft.textContent = label; }
}
function renderLists(){
  var urgent = activeMails().filter(function(m){return m.urgent;});
  $("#urgentList").innerHTML = urgent.length ? urgent.map(miniRowHtml).join("") : '<div class="empty">Nothing urgent right now.</div>';
  $("#recentList").innerHTML = activeMails().slice(0,5).map(mailRowHtml).join("");
  var list = filteredMails();
  var emptyMsg = state.query ? "No mail matches your search." : (state.cat==="social" ? "No social mail here." : state.cat==="promotions" ? "No promotions here." : "Your Primary tab is empty.");
  $("#inboxList").innerHTML = list.length ? list.map(gmRowHtml).join("") : '<div class="gm-empty">'+emptyMsg+'</div>';
  renderGmailChrome();
}
function currentMail(){
  for(var i=0;i<MAILS.length;i++){ if(MAILS[i].id===state.selectedId) return MAILS[i]; }
  return null;
}
function renderReader(){
  var listEl=$("#gmailList"), readEl=$("#gmailRead"), box=$("#reader");
  var m = state.reading ? currentMail() : null;
  if(!m || m.trashed || m.archived){
    state.reading=false;
    if(listEl) listEl.hidden=false;
    if(readEl) readEl.hidden=true;
    return;
  }
  if(listEl) listEl.hidden=true;
  if(readEl) readEl.hidden=false;
  var list=filteredMails();
  var idx=list.indexOf(m);
  var rr=$("#gmReadRange");
  if(rr) rr.textContent = idx>-1 ? (idx+1)+" of "+list.length : "";
  var pv=$("#gmPrev"), nx=$("#gmNext");
  if(pv) pv.disabled = idx<=0;
  if(nx) nx.disabled = idx<0 || idx>=list.length-1;
  /* Body rendering: full-body paragraphs are escaped at load time and
     already contain <br> separators (m._bodyIsHtml), so they are NOT
     escaped again here; the snippet fallback is raw text and IS escaped.
     HTML-only mail renders through the sanitiser into .gm-body-html. */
  var bodyHtml;
  if(m.bodyHtml){
    bodyHtml = '<div class="gm-body-html">' + m.bodyHtml + '</div>';
  } else if(m._bodyIsHtml){
    bodyHtml = (m.body || []).map(function(p){return "<p>"+p+"</p>";}).join("");
  } else {
    bodyHtml = (m.body || [m.snippet || ""]).map(function(p){return "<p>"+escapeHtml(p)+"</p>";}).join("");
  }
  box.innerHTML =
    '<div class="gm-subject-row"><h2>'+escapeHtml(m.subject)+'</h2><span class="gm-labelchip">Inbox</span><span class="gm-labelchip">'+escapeHtml(m.tag)+'</span></div>'
    + '<div class="gm-from"><span class="avatar" style="background:'+avatarColor(m.sender)+'">'+initials(m.sender)+'</span>'
    + '<div class="gm-from-main"><div class="gm-from-line1"><span class="gm-from-name">'+escapeHtml(m.sender)+'</span><span class="gm-from-email">&lt;'+escapeHtml(m.email)+'&gt;</span>'
    + '<span class="gm-from-right"><span class="gm-from-date num">'+escapeHtml(m.time)+'</span>'
    + '<button class="gm-star'+(m.starred?" on":"")+'" data-star="'+m.id+'" aria-label="Star this mail">'+(m.starred?ICO.starF:ICO.starO)+'</button>'
    + '<button class="gm-tbtn sm" id="gmHeadReply" aria-label="Reply">'+ICO.reply+'</button></span></div>'
    + '<div class="gm-from-to">to me</div></div></div>'
    + '<div class="gm-ai"><h3><svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"/></svg>AI summary</h3><p>'+escapeHtml(m.summary)+'</p></div>'
    + '<div class="gm-body">'+bodyHtml+'</div>'
    + '<div class="gm-reply-btns"><button class="gm-pill" id="gmReplyBtn">'+ICO.reply+'Reply</button>'
    + '<button class="gm-pill" id="gmForwardBtn">'+ICO.forward+'Forward</button></div>'
    + '<div class="gm-replybox" id="gmReplyBox" hidden><textarea id="replyText" placeholder="Reply to '+escapeHtml(m.sender)+'…"></textarea>'
    + '<div class="gm-reply-actions"><button class="gm-send" id="replyMailto">Send</button>'
    + '<button class="gm-textbtn" id="replyDraft">Draft with AI</button>'
    + '<button class="gm-textbtn" id="replyCopy">Copy text</button></div></div>';
  function openReplyBox(){
    var rb=$("#gmReplyBox"); if(rb) rb.hidden=false;
    var rt=$("#replyText"); if(rt) rt.focus();
  }
  var rb1=$("#gmReplyBtn"); if(rb1) rb1.addEventListener("click",openReplyBox);
  var rb2=$("#gmHeadReply"); if(rb2) rb2.addEventListener("click",openReplyBox);
  var fw=$("#gmForwardBtn");
  if(fw) fw.addEventListener("click",function(){
    /* Forward opens the compose box prefilled (sent via the Gmail API
       on submit), instead of a mailto: draft. */
    var to=$("#cTo"), sub=$("#cSubject"), bod=$("#cBody");
    if(to) to.value="";
    if(sub) sub.value="Fwd: "+m.subject;
    if(bod) bod.value="\n\n---------- Forwarded message ----------\nFrom: "+m.sender+" <"+m.email+">\nSubject: "+m.subject+"\n\n"+(m._bodyText || m.snippet || "");
    openCompose();
  });
  var draftBtn=$("#replyDraft");
  if(draftBtn) draftBtn.addEventListener("click",function(){
    var rt=$("#replyText"); if(rt) rt.value = draftReply(m);
    toast("Draft added — edit it before sending");
  });
  var mailBtn=$("#replyMailto");
  if(mailBtn) mailBtn.addEventListener("click",async function(){
    var v=$("#replyText").value.trim();
    if(!v){ toast("Write a reply first"); return; }
    mailBtn.disabled=true; mailBtn.textContent="Sending…";
    try {
      var res = await apiFetch("/api/mails/" + m.id + "/reply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body: v }) });
      var d = await res.json().catch(function(){ return {}; });
      if(res.ok){ toast("Reply sent"); var rb=$("#gmReplyBox"); if(rb) rb.hidden=true; $("#replyText").value=""; }
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
    MAILS.forEach(function(m){
      stateData.starred[m.id] = !!m.starred;
      stateData.read[m.id] = !m.unread;
      stateData.archived[m.id] = !!m.archived;
      stateData.trashed[m.id] = !!m.trashed;
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
    MAILS.forEach(function(m){
      if(s.starred && s.starred[m.id] !== undefined) m.starred = !!s.starred[m.id];
      if(s.read && s.read[m.id] !== undefined) m.unread = !s.read[m.id];
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
  renderLists();
}
function setCategory(c){
  state.cat=c;
  state.filter="all";
  state.selected={};
  renderLists();
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

async function bulkStateToBackend(ids, payload){
  /* One Gmail batchModify for many mails; returns true only when the
     server accepted the change, so callers never flip local state on a
     failed sync. */
  try {
    var res = await apiFetch("/api/mails/bulk-state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ ids: ids }, payload))
    });
    var d = await res.json().catch(function(){ return {}; });
    if(!res.ok){ toast(d.detail || "Couldn't sync that change with Gmail"); return false; }
    if(!d.synced_gmail){ toast("Gmail didn't accept that change — nothing was updated"); return false; }
    return true;
  } catch(e){
    toast("Couldn't reach the server to sync that change");
    return false;
  }
}

/* Delegated clicks: open mail + star + Gmail row controls */
async function loadFullBody(m){
  /* The inbox list only carries the snippet. Fetch the real body once
     per mail (GET /api/mails/{id}) and re-render the reader. Plain text
     is preferred; HTML-only mail falls back to sanitised HTML. */
  if(!m || m._fullLoading || m._fullLoaded) return;
  m._fullLoading = true;
  try {
    var res = await apiFetch("/api/mails/" + m.id);
    var d = await res.json();
    if(res.ok && d.status === "success" && d.mail){
      var txt = (d.mail.body_text || "").trim();
      if(txt){
        /* Keep the raw text for forwarding; the rendered paragraphs
           below are the escaped version. */
        m._bodyText = txt;
        /* Escape first, then turn newlines into <br> — the <br> stays
           live HTML while the mail text itself cannot inject markup. */
        m.body = txt.split(/\n{2,}/).map(function(p){ return escapeHtml(p).replace(/\n/g, "<br>"); });
        m.bodyHtml = "";
        m._bodyIsHtml = true;
      } else if(d.mail.body_html){
        m.body = [];
        m.bodyHtml = stripUnsafeHtml(d.mail.body_html);
      }
      if(d.mail.label_ids) m.labels = d.mail.label_ids.join(",");
      m._fullLoaded = true;
      if(state.reading && currentMail() === m) renderReader();
    }
  } catch(e){
    console.log("Full body load note:", e);
  } finally {
    m._fullLoading = false;
  }
}
function openMail(id){
  state.selectedId=id;
  state.reading=true;
  var opened = null;
  MAILS.forEach(function(m){
    if(m.id===id){
      m.unread=false;
      opened = m;
      syncStateToBackend(m.id, { is_unread: false });
    }
  });
  if(!opened && SEARCH_RESULTS){
    SEARCH_RESULTS.forEach(function(m){ if(m.id===id){ opened = m; m.unread=false; syncStateToBackend(m.id, { is_unread: false }); } });
  }
  renderAll();
  showView("inbox");
  if(opened) loadFullBody(opened);
}
function mailById(id){
  for(var i=0;i<MAILS.length;i++){ if(MAILS[i].id===id) return MAILS[i]; }
  if(SEARCH_RESULTS){ for(var j=0;j<SEARCH_RESULTS.length;j++){ if(SEARCH_RESULTS[j].id===id) return SEARCH_RESULTS[j]; } }
  return null;
}
function rowAction(act, m){
  if(!m) return;
  if(act==="archive"){
    syncStateToBackend(m.id, { is_archived: true }).then(function(ok){
      if(!ok) return;
      m.archived=true; if(state.selectedId===m.id) state.reading=false; delete state.selected[m.id]; renderAll();
      toast("Conversation archived");
    });
    return;
  }
  else if(act==="trash"){
    syncStateToBackend(m.id, { is_trash: true }).then(function(ok){
      if(!ok) return;
      m.trashed=true; if(state.selectedId===m.id) state.reading=false; delete state.selected[m.id]; renderAll();
      toast("Conversation deleted");
    });
    return;
  }
  else if(act==="spam"){
    syncStateToBackend(m.id, { is_spam: true }).then(function(ok){
      if(!ok) return;
      m.archived=true; if(state.selectedId===m.id) state.reading=false; delete state.selected[m.id]; renderAll();
      toast("Reported as spam");
    });
    return;
  }
  else if(act==="read"){
    m.unread = !m.unread;
    syncStateToBackend(m.id, { is_unread: m.unread });
    toast(m.unread ? "Marked as unread" : "Marked as read");
  }
  else if(act==="snooze"){ toast("Snooze isn\u2019t available yet \u2014 this mail stays in your inbox"); }
  delete state.selected[m.id];
  renderAll();
}
document.addEventListener("click",function(e){
  var star=e.target.closest ? e.target.closest("[data-star]") : null;
  if(star){
    e.stopPropagation();
    var sid=parseInt(star.getAttribute("data-star"),10);
    MAILS.forEach(function(m){
      if(m.id===sid){
        m.starred=!m.starred;
        syncStateToBackend(m.id, { is_starred: m.starred });
      }
    });
    renderAll(); return;
  }
  var sel=e.target.closest ? e.target.closest("[data-select]") : null;
  if(sel){
    e.stopPropagation();
    var cid=parseInt(sel.getAttribute("data-select"),10);
    state.selected[cid]=!state.selected[cid];
    renderLists(); renderReader(); return;
  }
  var act=e.target.closest ? e.target.closest("[data-act]") : null;
  if(act){
    e.stopPropagation();
    rowAction(act.getAttribute("data-act"), mailById(parseInt(act.getAttribute("data-id"),10)));
    return;
  }
  var open=e.target.closest ? e.target.closest("[data-open]") : null;
  if(open){
    openMail(parseInt(open.getAttribute("data-open"),10));
  }
});
async function markAll(){
  var unreadIds = activeMails().filter(function(m){ return m.unread; }).map(function(m){ return m.id; });
  if(!unreadIds.length){ toast("Nothing unread"); return; }
  var ok = await bulkStateToBackend(unreadIds, { is_unread: false });
  if(!ok) return;
  activeMails().forEach(function(m){ m.unread=false; });
  renderAll();
  toast("All mail marked as read");
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
      apiFetch("/api/mails?q=" + encodeURIComponent(q)).then(function(res){ return res.json(); }).then(function(d){
        if(d.status === "success" && Array.isArray(d.mails)){
          SEARCH_RESULTS = d.mails;
          SEARCH_RESULTS.forEach(function(m){ m.cat = m.cat || "primary"; m.archived = false; m.trashed = false; });
          renderLists(); renderReader();
        }
      }).catch(function(){});
    }, 350);
  } else {
    SEARCH_RESULTS = null;
  }
  renderLists(); renderReader();
});

/* ================= Compose ================= */
function openCompose(){ $("#scrim").classList.add("open"); $("#composeModal").classList.add("open"); var to=$("#cTo"); if(to) to.focus(); }
function closeCompose(){ $("#scrim").classList.remove("open"); $("#composeModal").classList.remove("open"); }
$("#composeBtn").addEventListener("click",openCompose);
$("#composeClose").addEventListener("click",closeCompose);
$("#scrim").addEventListener("click",function(){ closeCompose(); closeKeyModal(); });
document.addEventListener("keydown",function(e){
  if(e.key!=="Escape") return;
  closeCompose(); closeKeyModal();
  $("#sidebar").classList.remove("open"); $("#navScrim").classList.remove("open");
  closeMenus();
});
$("#composeForm").addEventListener("submit",async function(e){
  e.preventDefault();
  var to=$("#cTo").value.trim(), sub=$("#cSubject").value.trim(), body=$("#cBody").value;
  if(!to){ toast("Add a recipient first"); return; }
  if(!sub){ toast("Add a subject first"); return; }
  if(!body.trim()){ toast("Write your message first"); return; }
  try {
    var res = await apiFetch("/api/mails/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to: to, subject: sub, body: body }) });
    var d = await res.json().catch(function(){ return {}; });
    if(res.ok){
      toast("Sent");
      $("#cTo").value=""; $("#cSubject").value=""; $("#cBody").value="";
      closeCompose();
    } else {
      /* Keep the draft open so nothing is lost; Copy draft still works. */
      toast(d.detail || "Couldn't send the email");
    }
  } catch(err){ toast("Couldn't send the email — check connection"); }
});
$("#copyDraft").addEventListener("click",function(){
  var txt="To: "+$("#cTo").value+"\nSubject: "+$("#cSubject").value+"\n\n"+$("#cBody").value;
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
  filteredMails().forEach(function(m){ if(fn(m)) state.selected[m.id]=true; });
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
  var allSel=list.length>0 && list.every(function(m){ return state.selected[m.id]; });
  if(allSel){ list.forEach(function(m){ delete state.selected[m.id]; }); }
  else { list.forEach(function(m){ state.selected[m.id]=true; }); }
  renderLists();
});
$("#gmMaster2").addEventListener("click",function(){ state.selected={}; renderLists(); });

$("#gmFilterClear").addEventListener("click",function(){
  state.filter="all"; state.query=""; state.selected={};
  SEARCH_RESULTS = null;
  var si=$("#searchInput"); if(si) si.value="";
  renderLists();
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
    LABELS_CACHE = (res.ok && d.status === "success") ? (d.labels || []) : [];
  } catch(e){ LABELS_CACHE = []; }
  return LABELS_CACHE;
}
function mailLabelIds(m){
  return (m.labels || "").split(",").map(function(s){ return s.trim(); }).filter(Boolean);
}
async function openLabelMenu(anchorBtn, ids){
  var menu = $("#gmLabelMenu");
  if(!menu) return;
  var labels = await loadLabels();
  closeMenus();
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
    try {
      var res = await apiFetch("/api/mails/" + ids[i] + "/labels", {
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
    var payload = act==="archive" ? { is_archived: true } : act==="trash" ? { is_trash: true } : act==="spam" ? { is_spam: true } : { is_unread: false };
    bulkStateToBackend(ids, payload).then(function(ok){
      if(!ok) return;
      ids.forEach(function(id){
        var m=mailById(id); if(!m) return;
        if(act==="archive" || act==="spam") m.archived=true;
        else if(act==="trash") m.trashed=true;
        else if(act==="read") m.unread=false;
      });
      state.selected={};
      renderAll();
      toast(act==="archive" ? "Conversations archived" : act==="read" ? "Marked as read" : act==="spam" ? "Reported as spam" : "Conversations deleted");
    });
  });
});
/* Reading toolbar */
$("#gmBack").addEventListener("click",function(){ state.reading=false; renderAll(); });
function currentAction(act){ rowAction(act, currentMail()); }
$("#gmReadArchive").addEventListener("click",function(){ currentAction("archive"); });
$("#gmReadTrash").addEventListener("click",function(){ currentAction("trash"); });
$("#gmReadSpam").addEventListener("click",function(){ currentAction("spam"); });
$("#gmReadSnooze").addEventListener("click",function(){ toast("Snooze isn\u2019t available yet \u2014 this mail stays in your inbox"); state.reading=false; renderAll(); });
$("#gmReadUnread").addEventListener("click",function(){
  var m=currentMail(); if(m){ m.unread=true; syncStateToBackend(m.id, { is_unread: true }); } state.reading=false; renderAll();
});
$("#gmReadLabel").addEventListener("click",function(){
  var m=currentMail(); if(m) openLabelMenu(this, [m.id]);
});
function stepMail(dir){
  var list=filteredMails();
  var m=currentMail(); if(!m) return;
  var idx=list.indexOf(m);
  var next=list[idx+dir];
  if(next) openMail(next.id);
}
$("#gmPrev").addEventListener("click",function(){ stepMail(-1); });
$("#gmNext").addEventListener("click",function(){ stepMail(1); });

/* ================= Live Backend Integration ================= */
async function initBackend(){
  var dchip = $("#currentDateChip");
  if(dchip){
    dchip.textContent = new Date().toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  }

  try {
    var res = await apiFetch("/api/dashboard");
    var data = await res.json();
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

  // Load real emails from TiDB
  try {
    var mres = await apiFetch("/api/mails");
    var mdata = await mres.json();
    if(mdata.status === "success" && Array.isArray(mdata.mails)){
      MAILS = mdata.mails;
      MAILS.forEach(function(m){
        m.cat = m.cat || "primary";
        m.archived = false;
        m.trashed = false;
      });
      applyLocalState();
      renderAll();
    }
  } catch(e){
    console.log("Database mails query:", e);
  }
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

// Auto-fill default model on provider change per rule 1.3
var provSelect = $("#keyProvider");
if(provSelect){
  provSelect.addEventListener("change", function(){
    var km = $("#keyModel");
    if(km){
      if(this.value === "gemini") km.value = "gemini-3.5-flash";
      else if(this.value === "groq") km.value = "openai/gpt-oss-120b";
      else if(this.value === "openai") km.value = "gpt-4o-mini";
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
