#!/usr/bin/env python3
"""Local live dashboard for MAP.md and EVENTS.jsonl."""

from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import secrets
import socket
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__DASHBOARD_LABEL__ · 실시간 침투 지도</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#070b12; --panel:#0d1420; --panel2:#111b2a; --line:#223149;
      --text:#e8eef7; --muted:#8291a8; --cyan:#56d9e8; --green:#5fe39a;
      --amber:#f4c56a; --red:#ff7b86; --violet:#a99cff;
    }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; color:var(--text); font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:radial-gradient(circle at 85% -10%,rgba(45,129,196,.18),transparent 36rem),var(--bg); }
    .shell { width:min(1600px,100%); margin:auto; padding:26px; }
    header { display:flex; justify-content:space-between; align-items:flex-end; gap:18px; margin-bottom:20px; }
    .eyebrow { color:var(--cyan); font-size:12px; font-weight:800; letter-spacing:.16em; text-transform:uppercase; }
    h1 { margin:5px 0 0; font-size:clamp(26px,4vw,42px); letter-spacing:-.04em; }
    .connection { display:flex; align-items:center; gap:9px; color:var(--muted); font-size:13px; }
    .dot { width:9px; height:9px; border-radius:50%; background:var(--amber); box-shadow:0 0 16px currentColor; }
    .dot.live { color:var(--green); background:var(--green); } .dot.error { color:var(--red); background:var(--red); }
    .stats { display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:10px; margin-bottom:14px; }
    .stat,.card { border:1px solid var(--line); background:linear-gradient(180deg,rgba(17,27,42,.96),rgba(10,16,26,.96)); box-shadow:0 18px 60px rgba(0,0,0,.2); }
    .stat { border-radius:12px; padding:13px 14px; min-width:0; }
    .stat-label { color:var(--muted); font-size:10px; letter-spacing:.08em; text-transform:uppercase; white-space:nowrap; }
    .stat-value { margin-top:7px; font:700 23px/1 ui-monospace,SFMono-Regular,Menlo,monospace; overflow:hidden; text-overflow:ellipsis; }
    .green{color:var(--green)} .amber{color:var(--amber)} .red{color:var(--red)} .cyan{color:var(--cyan)} .violet{color:var(--violet)}
    .grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr); gap:14px; align-items:start; }
    .card { border-radius:15px; overflow:hidden; }
    .card-head { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:13px 16px; border-bottom:1px solid var(--line); background:rgba(7,11,18,.45); }
    .card-title { font-weight:800; } .card-meta { color:var(--muted); font-size:11px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .stack { display:grid; gap:14px; }
    .panel-body { padding:14px; }
    .focus { padding:16px; border:1px solid rgba(86,217,232,.38); background:rgba(86,217,232,.065); border-radius:11px; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; }
    .list { display:grid; gap:8px; max-height:300px; overflow:auto; }
    .row { border:1px solid var(--line); border-radius:10px; padding:10px 11px; background:rgba(7,11,18,.35); }
    .row-top { display:flex; align-items:center; gap:8px; margin-bottom:5px; }
    .row-id { color:var(--cyan); font:700 12px ui-monospace,SFMono-Regular,Menlo,monospace; }
    .row-text { color:#cbd7e8; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
    .badge { border:1px solid var(--line); border-radius:999px; padding:3px 7px; color:var(--muted); font-size:10px; font-weight:800; }
    .badge.focus,.badge.ok { color:var(--green); border-color:rgba(95,227,154,.38); }
    .badge.open,.badge.candidate { color:var(--cyan); border-color:rgba(86,217,232,.38); }
    .badge.parked,.badge.running { color:var(--amber); border-color:rgba(244,197,106,.38); }
    .badge.closed,.badge.failed { color:var(--red); border-color:rgba(255,123,134,.38); }
    .sync-card { margin-bottom:14px; }
    .sync-card.warn { border-color:rgba(255,123,134,.58); }
    .sync-card.ok { border-color:rgba(95,227,154,.4); }
    .sync-summary { padding:12px 14px; border-radius:10px; border:1px solid var(--line); color:#cbd7e8; font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; }
    .sync-card.warn .sync-summary { border-color:rgba(255,123,134,.4); background:rgba(255,123,134,.07); }
    .sync-card.ok .sync-summary { border-color:rgba(95,227,154,.32); background:rgba(95,227,154,.05); }
    .empty { color:var(--muted); font-size:12px; padding:10px 2px; }
    .toolbar { display:flex; align-items:center; gap:8px; flex:0 0 auto; }
    button { border:1px solid var(--line); border-radius:8px; padding:7px 10px; background:var(--panel2); color:var(--text); cursor:pointer; font-weight:750; }
    button.active { border-color:var(--cyan); color:var(--cyan); }
    button:disabled { opacity:.4; cursor:default; }
    #mapViewport { overflow:auto; height:600px; }
    pre { margin:0; padding:20px; min-width:max-content; white-space:pre; color:#d9e5f5; font:14px/1.7 ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; tab-size:2; }

    /* 고정 배치: 각 칸은 자기 자리에 머무르고 칸 안에서만 스크롤한다. */
    .board { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(340px,.65fr); gap:14px; align-items:start; }
    .col { display:grid; gap:14px; min-width:0; }
    .pair { display:grid; grid-template-columns:1fr 1fr; gap:14px; min-width:0; }
    .list.tall { max-height:560px; }
    .list.short { max-height:230px; }
    .hint { color:var(--muted); font-size:11px; font-weight:600; margin-left:7px; }
    .count { color:var(--cyan); font-size:11px; font-weight:800; }

    /* 새로 올라온 항목만 눈에 띄게. 스크롤은 건드리지 않는다. */
    .row.fresh { border-color:rgba(86,217,232,.65); background:rgba(86,217,232,.08); }
    .badge.new { color:var(--cyan); border-color:rgba(86,217,232,.6); background:rgba(86,217,232,.12); }
    details.raw { margin-top:14px; border:1px solid var(--line); border-radius:15px; background:linear-gradient(180deg,rgba(17,27,42,.96),rgba(10,16,26,.96)); }
    details.raw > summary { cursor:pointer; padding:13px 16px; font-weight:800; list-style:none; display:flex; justify-content:space-between; gap:12px; align-items:center; }
    details.raw > summary::-webkit-details-marker { display:none; }
    details.raw[open] > summary { border-bottom:1px solid var(--line); }

    @media(max-width:1100px){.stats{grid-template-columns:repeat(4,minmax(0,1fr))}.board{grid-template-columns:1fr}.pair{grid-template-columns:1fr}}
    @media(max-width:650px){.shell{padding:16px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}header{align-items:flex-start;flex-direction:column}.card-head{align-items:flex-start;flex-direction:column}#mapViewport{height:520px}pre{font-size:12px}.list.tall{max-height:380px}}
  </style>
</head>
<body>
<main class="shell">
  <header>
    <div><div class="eyebrow">Structurally synchronized live control view</div><h1>__DASHBOARD_LABEL__ 실시간 침투 지도</h1></div>
    <div class="connection"><span id="dot" class="dot"></span><span id="status">연결 중</span></div>
  </header>

  <section class="stats" aria-label="진행 요약">
    <div class="stat"><div class="stat-label">마지막 변경</div><div id="updated" class="stat-value" style="font-size:15px">—</div></div>
    <div class="stat"><div class="stat-label">행동 E</div><div id="events" class="stat-value violet">0</div></div>
    <div class="stat"><div class="stat-label">단서 C</div><div id="clues" class="stat-value">0</div></div>
    <div class="stat"><div class="stat-label">확인 #</div><div id="confirmed" class="stat-value green">0</div></div>
    <div class="stat"><div class="stat-label">가정 ?</div><div id="hypotheses" class="stat-value amber">0</div></div>
    <div class="stat"><div class="stat-label">현재 FOCUS</div><div id="focusCount" class="stat-value green">0</div></div>
    <div class="stat"><div class="stat-label">열린 가지</div><div id="openCount" class="stat-value cyan">0</div></div>
    <div class="stat"><div class="stat-label">미승격 후보</div><div id="candidateCount" class="stat-value amber">0</div></div>
  </section>

  <section class="board">
    <div class="col">
      <section class="card" id="cluesCard">
        <div class="card-head">
          <div class="card-title">상승 경로 <span class="hint">최신이 맨 위</span></div>
          <div id="clueMeta" class="card-meta">C-* 대기 중</div>
          <div class="toolbar"><button id="clueJump" type="button" disabled>새 항목 0</button></div>
        </div>
        <div class="panel-body"><div id="clueList" class="list tall"></div></div>
      </section>

      <div class="pair">
        <section class="card">
          <div class="card-head"><div class="card-title">미승격 관찰</div><div class="card-meta">candidate / closed</div></div>
          <div class="panel-body"><div id="candidates" class="list short"></div></div>
        </section>
        <section id="syncCard" class="card">
          <div class="card-head"><div class="card-title">기록 동기화 감시</div><div id="syncState" class="card-meta">확인 중</div></div>
          <div class="panel-body"><div id="syncSummary" class="sync-summary">EVENTS · MAP · LEDGER를 비교하고 있습니다.</div><div style="height:10px"></div><div id="recovered" class="list short"></div></div>
        </section>
      </div>
    </div>

    <div class="col">
      <section id="targetCard" class="card">
        <div class="card-head"><div class="card-title">대상 범위 승인</div><div id="targetMeta" class="card-meta">확인 중</div></div>
        <div class="panel-body"><div id="targetPending" class="list short"></div><div style="height:10px"></div><div id="targetApproved" class="list short"></div></div>
      </section>

      <section class="card">
        <div class="card-head"><div class="card-title">실시간 행동</div><div id="eventPath" class="card-meta">EVENTS.jsonl 대기 중</div></div>
        <div class="panel-body"><div id="activity" class="list short"></div></div>
      </section>

      <section class="card">
        <div class="card-head"><div class="card-title">현재 초점과 탐색 가지</div><div id="branchMeta" class="card-meta">B-* 대기 중</div></div>
        <div class="panel-body"><div id="focus" class="focus">FOCUS 가지가 생성되면 여기에 표시됩니다.</div><div style="height:10px"></div><div id="branches" class="list short"></div></div>
      </section>
    </div>
  </section>

  <details class="raw">
    <summary><span>전체 MAP.md 원문</span><span id="mapPath" class="card-meta">MAP.md 대기 중</span></summary>
    <div class="card-head" style="border-bottom:none"><div class="card-meta">원문은 자동으로 스크롤하지 않는다</div><div class="toolbar"><button id="smaller" type="button">A−</button><button id="larger" type="button">A+</button><button id="top" type="button">맨 위</button></div></div>
    <div id="mapViewport"><pre id="map" class="empty">MAP.md가 생성되거나 변경되면 자동 표시됩니다.</pre></div>
  </details>
</main>

<script>
  const el=id=>document.getElementById(id), mapEl=el('map'), viewport=el('mapViewport');
  let lastVersion='', fontSize=14;
  const pendingFresh={clue:0};
  const lines=text=>text.split(/\r?\n/);
  const statusOf=line=>((line.match(/\[(FOCUS|OPEN|PARKED|CLOSED)\]/i)||[])[1]||'').toUpperCase();
  const candidateState=line=>((line.match(/\[(unreviewed|candidate|promoted|closed)\]/i)||[])[1]||'').toLowerCase();
  const eventNumber=value=>{const m=String(value||'').match(/^E-(\d+)/i);return m?Number(m[1]):-1};
  const idsIn=(text,prefix)=>new Set(Array.from(String(text||'').matchAll(new RegExp(`\\b${prefix}-\\d+\\b`,'gi')),m=>m[0].toUpperCase()));

  function eventSnapshot(events){
    const records=new Map();
    events.forEach(event=>{const key=event.event_id;if(!key)return;const record=records.get(key)||{event_id:key,start:null,finish:null,last:null};if(event.phase==='start')record.start=event;else record.finish=event;record.last=event;records.set(key,record)});
    const ordered=Array.from(records.values()).sort((a,b)=>eventNumber(a.event_id)-eventNumber(b.event_id));
    const active=ordered.filter(record=>record.start&&!record.finish);
    const finished=ordered.filter(record=>record.finish);
    const live=(active.at(-1)||finished.at(-1)||{}).last||null;
    return {records,ordered,active,finished,live,latestFinished:(finished.at(-1)||{}).finish||null};
  }

  function eventTrail(live,snapshot){
    const trail=[];let current=live;const seen=new Set();
    while(current&&current.event_id&&!seen.has(current.event_id)&&trail.length<8){trail.push(current);seen.add(current.event_id);const parent=current.parent_event;const record=parent?snapshot.records.get(parent):null;current=record?(record.finish||record.start||record.last):null}
    return trail.reverse();
  }

  function empty(container,message){ container.replaceChildren(); const d=document.createElement('div'); d.className='empty'; d.textContent=message; container.appendChild(d); }
  function badge(value){ const s=document.createElement('span'); s.className='badge '+String(value).toLowerCase(); s.textContent=value; return s; }

  // 새로고침해도 보던 자리를 지킨다. 위치 이동은 사용자가 버튼을 눌렀을 때만 일어난다.
  function keepScroll(box,paint){ const top=box.scrollTop, left=box.scrollLeft; paint(); box.scrollTop=top; box.scrollLeft=left; }

  // 칸별로 이미 본 항목을 기억해 새로 올라온 것만 표시한다.
  const seen={clue:new Set(),branch:new Set(),cand:new Set(),act:new Set(),target:new Set()};
  const primed={};
  function markFresh(kind,id,row){
    if(!id)return false;
    const store=seen[kind];
    if(store.has(id))return false;
    store.add(id);
    if(!primed[kind])return false;      // 첫 렌더에서는 전부 새 항목으로 보이지 않게 한다
    row.classList.add('fresh');
    row.prepend(badge('NEW'));
    return true;
  }
  function prime(kind){ primed[kind]=true; }

  function makeRow(id,state,text){
    const row=document.createElement('div');row.className='row';
    const top=document.createElement('div');top.className='row-top';
    const rid=document.createElement('span');rid.className='row-id';rid.textContent=id;
    top.append(rid,badge(state));
    const body=document.createElement('div');body.className='row-text';body.textContent=text;
    row.append(top,body);
    return {row,top};
  }

  function renderClues(text){
    const box=el('clueList');
    // MAP의 상승 경로는 최신이 맨 위다. 순서를 그대로 유지한다.
    const rows=lines(text).filter(line=>/^C-\d+\s*\|/i.test(line));
    keepScroll(box,()=>{
      box.replaceChildren();
      if(!rows.length){empty(box,'단서가 승격되면 최신 항목이 이 칸 맨 위에 쌓입니다.');return;}
      let fresh=0;
      rows.forEach(line=>{
        const id=(line.match(/^C-\d+/i)||['C'])[0].toUpperCase();
        const existence=/\|\s*#\s*\|/.test(line)?'확인':/\|\s*\?\s*\|/.test(line)?'가정':'관찰';
        const {row,top}=makeRow(id,existence,line);
        if(markFresh('clue',id,top))fresh++;
        if(/<현재 위치>/.test(line))row.classList.add('fresh');
        box.appendChild(row);
      });
      const button=el('clueJump');
      pendingFresh.clue+=fresh;
      button.textContent='새 항목 '+pendingFresh.clue;
      button.disabled=pendingFresh.clue===0;
      button.classList.toggle('active',pendingFresh.clue>0);
      el('clueMeta').textContent=`C-* ${rows.length}개 · 확인 ${rows.filter(l=>/\|\s*#\s*\|/.test(l)).length} · 가정 ${rows.filter(l=>/\|\s*\?\s*\|/.test(l)).length}`;
    });
    prime('clue');
  }
  function renderBranches(text,events){
    const all=lines(text).filter(line=>/^B-\d+\s*\|/i.test(line));
    const focus=all.find(line=>statusOf(line)==='FOCUS');
    const snapshot=eventSnapshot(events), latest=snapshot.latestFinished, mapEventIds=idsIn(text,'E');
    const latestMap=Math.max(-1,...Array.from(mapEventIds,eventNumber)), latestFinished=eventNumber(latest&&latest.event_id), stale=latestFinished>latestMap;
    if(stale||!focus){
      const live=snapshot.live, state=live?(live.phase==='start'?'RUNNING':String(live.status||live.phase||'EVENT').toUpperCase()):'WAITING';
      const path=eventTrail(live,snapshot).map(event=>event.event_id).join(' → ');
      const liveLine=live?`EVENTS 자동 추적 | ${live.event_id} [${state}] | ${live.action_type||''}${live.scope_ref?' | '+live.scope_ref:''}`:'EVENTS 자동 추적 대기 중';
      el('focus').textContent=`${liveLine}${path?'\n경로: '+path:''}${focus?'\nMAP 기준: '+focus:'\nB-* 의미 분류는 Claude 기록 대기 중'}`;
    }else el('focus').textContent=focus;
    const others=all.filter(line=>line!==focus);
    const counts={FOCUS:0,OPEN:0,PARKED:0,CLOSED:0}; all.forEach(line=>{const s=statusOf(line);if(s)counts[s]++});
    el('focusCount').textContent=counts.FOCUS; el('openCount').textContent=counts.OPEN;
    const lag=stale?` · MAP ${latestFinished-latestMap}단계 지연`:'';
    el('branchMeta').textContent=`FOCUS ${counts.FOCUS} · OPEN ${counts.OPEN} · PARKED ${counts.PARKED} · CLOSED ${counts.CLOSED}${lag}`;
    const box=el('branches');
    keepScroll(box,()=>{
      box.replaceChildren();
      if(!others.length){empty(box,'다른 OPEN/PARKED/CLOSED 가지가 아직 없습니다.');return;}
      others.forEach(line=>{const id=(line.match(/^B-\d+/i)||['B'])[0].toUpperCase();const {row,top}=makeRow(id,statusOf(line)||'BRANCH',line);markFresh('branch',id,top);box.appendChild(row)});
    });
    prime('branch');
  }

  function renderCandidates(text,events){
    const byId=new Map();
    lines(text).filter(line=>/^E-\d+\s*\|\s*\[(unreviewed|candidate|closed)\]/i.test(line)).forEach(line=>byId.set((line.match(/^E-\d+/i)||[''])[0].toUpperCase(),line));
    events.filter(event=>event.phase!=='start'&&['unreviewed','candidate','closed'].includes(String(event.promotion_state||'').toLowerCase())).forEach(event=>{if(!byId.has(event.event_id))byId.set(event.event_id,`${event.event_id} | [${event.promotion_state}] | ${event.observation_summary||event.action_type||'관찰'} | EVENTS 자동 표시`)});
    const items=Array.from(byId.values()).sort((a,b)=>eventNumber((a.match(/^E-\d+/i)||[''])[0])-eventNumber((b.match(/^E-\d+/i)||[''])[0]));
    el('candidateCount').textContent=items.filter(line=>candidateState(line)==='candidate'||candidateState(line)==='unreviewed').length;
    const box=el('candidates');
    keepScroll(box,()=>{
      box.replaceChildren();
      if(!items.length){empty(box,'미승격 관찰이 생기면 여기에 표시됩니다.');return;}
      items.slice(-20).reverse().forEach(line=>{const id=(line.match(/^E-\d+/i)||['E'])[0].toUpperCase();const {row,top}=makeRow(id,candidateState(line)||'candidate',line);markFresh('cand',id,top);box.appendChild(row)});
    });
    prime('cand');
  }

  function renderSync(text,data){
    const events=data.events||[], mapClues=idsIn(text,'C'), ledgerClues=idsIn(data.ledger_content||'','C'), promoted=new Map();
    events.filter(event=>event.phase!=='start'&&String(event.promotion_state||'').toLowerCase()==='promoted'&&Array.isArray(event.clue_ids)).forEach(event=>event.clue_ids.forEach(clue=>promoted.set(String(clue).toUpperCase(),event)));
    const missingMap=Array.from(promoted.keys()).filter(clue=>!mapClues.has(clue)).sort(), missingLedger=Array.from(promoted.keys()).filter(clue=>!ledgerClues.has(clue)).sort();
    const snapshot=eventSnapshot(events), latestFinished=eventNumber(snapshot.latestFinished&&snapshot.latestFinished.event_id), latestMap=Math.max(-1,...Array.from(idsIn(text,'E'),eventNumber)), lag=Math.max(0,latestFinished-latestMap);
    const issues=missingMap.length+missingLedger.length+lag;
    const card=el('syncCard');card.classList.toggle('warn',issues>0);card.classList.toggle('ok',issues===0);
    el('syncState').textContent=issues?`지연 감지 · MAP 누락 ${missingMap.length} · LEDGER 누락 ${missingLedger.length}`:'동기화됨';
    const live=snapshot.live, liveText=live?`${live.event_id} ${live.phase==='start'?'실행 중':(live.status||live.phase||'완료')} · ${live.stage_id||'미지정'} · ${live.action_type||''}`:'행동 대기 중';
    el('syncSummary').textContent=`최신 활동: ${liveText}\nMAP 기준 최신 E: ${latestMap<0?'없음':'E-'+String(latestMap).padStart(4,'0')} · 완료 이벤트 지연: ${lag}\n원본 MAP을 수정하지 않고 누락 항목을 아래에 자동 복구 표시합니다.`;
    const box=el('recovered');
    const missingAll=Array.from(new Set([...missingMap,...missingLedger])).sort();
    const paint=()=>{
    box.replaceChildren();
    if(!missingAll.length){empty(box,'EVENTS 기준 누락된 승격 단서가 없습니다.');return;}
    missingAll.forEach(clue=>{const event=promoted.get(clue)||{};const row=document.createElement('div');row.className='row';const top=document.createElement('div');top.className='row-top';const id=document.createElement('span');id.className='row-id';id.textContent=clue;top.append(id,badge('AUTO'));const body=document.createElement('div');body.className='row-text';const where=[missingMap.includes(clue)?'MAP 누락':'',missingLedger.includes(clue)?'LEDGER 누락':''].filter(Boolean).join(' · ');body.textContent=`${where}\n근거 ${event.event_id||'E'} · ${event.observation_summary||event.action_type||'승격 이벤트'}`;row.append(top,body);box.appendChild(row)});
    };
    keepScroll(box,paint);
  }

  // 이 페이지에서만 승인할 수 있게 하는 실행별 토큰. 커스텀 헤더라 크로스오리진
  // 요청은 프리플라이트를 거쳐야 하고, 서버가 CORS를 허용하지 않아 실패한다.
  const CSRF_TOKEN='__CSRF_TOKEN__';

  let busyTarget=null;
  async function decideTarget(id,action){
    if(busyTarget)return; busyTarget=id;
    try{
      const response=await fetch('/api/target',{method:'POST',headers:{'Content-Type':'application/json','X-Redteam-Token':CSRF_TOKEN},body:JSON.stringify({id,action})});
      if(!response.ok){const detail=await response.text();alert('처리 실패: '+detail)}
      lastVersion=''; await poll();
    }catch(error){alert('처리 실패: '+error.message)}
    finally{busyTarget=null}
  }

  function renderTargets(data){
    const targets=data.targets||{}, entries=Object.entries(targets).sort((a,b)=>a[0].localeCompare(b[0]));
    const waiting=entries.filter(([,t])=>t.status==='pending'), settled=entries.filter(([,t])=>t.status!=='pending');
    const approved=settled.filter(([,t])=>t.status==='approved');
    const card=el('targetCard');card.classList.toggle('warn',waiting.length>0);card.classList.toggle('ok',waiting.length===0&&approved.length>0);
    el('targetMeta').textContent=`승인 ${approved.length} · 대기 ${waiting.length} · 현재 ${data.current_stage||'stage1'}`;
    const box=el('targetPending');
    const paintPending=()=>{
    box.replaceChildren();
    if(!waiting.length)empty(box,'승인 대기 중인 대상이 없습니다. 새 IP가 발견되면 여기에 승인 버튼이 나타납니다.');
    else waiting.forEach(([id,t])=>{
      const row=document.createElement('div');row.className='row';
      const top=document.createElement('div');top.className='row-top';
      const rid=document.createElement('span');rid.className='row-id';rid.textContent=id;
      top.append(rid,badge('PENDING'));
      const body=document.createElement('div');body.className='row-text';
      body.textContent=`${t.value}\n근거: ${t.evidence||'없음'} · ${t.reason||''}`;
      const actions=document.createElement('div');actions.className='toolbar';actions.style.marginTop='9px';
      const ok=document.createElement('button');ok.type='button';ok.textContent='승인';ok.className='active';
      ok.addEventListener('click',()=>{if(confirm(`${t.value} 을(를) 범위에 추가하고 새 Stage를 시작할까요?`))decideTarget(id,'approve')});
      const no=document.createElement('button');no.type='button';no.textContent='거부';
      no.addEventListener('click',()=>{if(confirm(`${t.value} 을(를) 거부할까요? 이후에도 계속 차단됩니다.`))decideTarget(id,'reject')});
      actions.append(ok,no);
      row.append(top,body,actions);box.appendChild(row);
    });
    };
    keepScroll(box,paintPending);
    const done=el('targetApproved');done.replaceChildren();
    if(!settled.length)empty(done,'아직 확정된 대상이 없습니다.');
    else settled.forEach(([id,t])=>{
      const row=document.createElement('div');row.className='row';
      const top=document.createElement('div');top.className='row-top';
      const rid=document.createElement('span');rid.className='row-id';rid.textContent=id;
      top.append(rid,badge(t.status==='approved'?'OK':'CLOSED'));
      const body=document.createElement('div');body.className='row-text';
      body.textContent=`${t.value} · ${t.stage||'미배정'}\n${t.reason||''}`;
      row.append(top,body);done.appendChild(row);
    });
  }

  function renderActivity(events){
    const box=el('activity');
    keepScroll(box,()=>{
      box.replaceChildren();
      if(!events.length){empty(box,'EVENTS.jsonl이 생성되면 모든 행동이 시간순으로 표시됩니다.');return;}
      events.slice(-30).reverse().forEach(event=>{
        const id=event.event_id||'E';
        const state=event.phase==='start'?'running':(event.status||event.phase||'event');
        const parts=[event.ts_utc,event.stage_id,event.action_type,event.observation_summary,event.promotion_state,event.duration_ms==null?'':`${event.duration_ms}ms`].filter(Boolean);
        const {row,top}=makeRow(id,state,parts.join(' · '));
        markFresh('act',id+':'+state,top);
        box.appendChild(row);
      });
    });
    prime('act');
  }

  function updateStats(text,data){
    const nodeLines=lines(text).filter(line=>/^C-\d+\s*\|/i.test(line));
    const clueIds=new Set(nodeLines.map(line=>(line.match(/^C-\d+/i)||[''])[0].toUpperCase()));
    (data.events||[]).forEach(event=>{if(String(event.promotion_state||'').toLowerCase()==='promoted'&&Array.isArray(event.clue_ids))event.clue_ids.forEach(clue=>clueIds.add(String(clue).toUpperCase()))});
    el('clues').textContent=clueIds.size;
    el('confirmed').textContent=nodeLines.filter(line=>/\|\s*#\s*\|/.test(line)).length;
    el('hypotheses').textContent=nodeLines.filter(line=>/\|\s*\?\s*\|/.test(line)).length;
    el('events').textContent=data.event_count||0;
  }

  async function poll(){
    try{
      const response=await fetch('/api/state',{cache:'no-store'}); if(!response.ok)throw new Error('HTTP '+response.status);
      const data=await response.json(); el('dot').className='dot live'; el('status').textContent=data.map_exists?'실시간 연결됨':'MAP.md 생성 대기 중';
      el('mapPath').textContent=data.map_path; el('eventPath').textContent=data.events_path;
      const version=`${data.map_mtime_ns}:${data.map_size}:${data.events_mtime_ns}:${data.events_size}:${data.ledger_mtime_ns}:${data.ledger_size}:${data.state_mtime_ns}:${data.state_size}`;
      if(version!==lastVersion){
        const text=data.map_content||'';
        // 원문 칸도 보던 자리를 유지한다. 자동으로 맨 아래로 내려가지 않는다.
        keepScroll(viewport,()=>{mapEl.textContent=text||'MAP.md가 생성되거나 내용이 추가되기를 기다리고 있습니다.'; mapEl.className=text?'':'empty'});
        el('updated').textContent=data.updated||'—';
        updateStats(text,data); renderClues(text); renderTargets(data); renderSync(text,data);
        renderBranches(text,data.events||[]); renderCandidates(text,data.events||[]); renderActivity(data.events||[]);
        lastVersion=version;
      }
    }catch(error){el('dot').className='dot error';el('status').textContent='연결 재시도 중'}
  }
  el('clueJump').addEventListener('click',()=>{
    const box=el('clueList'), target=box.querySelector('.row.fresh');
    if(target)box.scrollTop=Math.max(0,target.offsetTop-box.offsetTop-8);
    box.querySelectorAll('.row.fresh').forEach(row=>{row.classList.remove('fresh');const b=row.querySelector('.badge.new');if(b)b.remove()});
    pendingFresh.clue=0; const button=el('clueJump'); button.textContent='새 항목 0'; button.disabled=true; button.classList.remove('active');
  });
  el('top').addEventListener('click',()=>{viewport.scrollTop=0});
  el('smaller').addEventListener('click',()=>{fontSize=Math.max(10,fontSize-1);mapEl.style.fontSize=fontSize+'px'});
  el('larger').addEventListener('click',()=>{fontSize=Math.min(24,fontSize+1);mapEl.style.fontSize=fontSize+'px'});
  poll(); setInterval(poll,750);
</script>
</body>
</html>
"""


SAFE_EVENT_FIELDS = (
    "event_id", "phase", "ts_utc", "action_type", "parent_event", "status",
    "exit_code", "duration_ms", "observation_summary", "promotion_state",
    "clue_ids", "map_changed", "evidence_path", "scope_ref", "branch_id",
    "agent_id", "action_label", "stage_id",
)


class DashboardHandler(BaseHTTPRequestHandler):
    map_path: Path
    events_path: Path
    ledger_path: Path
    state_path: Path
    page_bytes: bytes
    stage_filter: str | None
    csrf_token: str = ""
    allowed_origins: frozenset[str] = frozenset()

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            body = self.page_bytes
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if route == "/api/state":
            body = json.dumps(self._state(), ensure_ascii=False).encode("utf-8")
            self._headers(200, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        body = b"Not found"
        self._headers(404, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route != "/api/target":
            self._reply(404, {"error": "not found"})
            return
        # 로컬 대시보드에서만 승인할 수 있게 한다.
        #
        # client_address만으로는 부족하다. 사용자가 브라우저로 아무 사이트나 열어두면
        # 그 페이지가 127.0.0.1로 요청을 보낼 수 있고, Content-Type을 text/plain으로
        # 두면 프리플라이트도 없이 통과한다(CSRF). 그래서 실행별 토큰을 커스텀 헤더로
        # 요구하고 Origin·Sec-Fetch-Site도 함께 확인한다.
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self._reply(403, {"error": "local only"})
            return
        origin = self.headers.get("Origin")
        if origin and origin not in self.allowed_origins:
            self._reply(403, {"error": "bad origin"})
            return
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            self._reply(403, {"error": "cross-site request rejected"})
            return
        token = self.headers.get("X-Redteam-Token") or ""
        if not self.csrf_token or not hmac.compare_digest(token, self.csrf_token):
            self._reply(403, {"error": "bad or missing token; 대시보드 페이지에서 승인하세요"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, {"error": "bad length"})
            return
        if length <= 0 or length > 4096:
            self._reply(400, {"error": "bad length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"error": "bad json"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "bad payload"})
            return
        tid = str(payload.get("id") or "")
        action = str(payload.get("action") or "")
        if action not in {"approve", "reject"}:
            self._reply(400, {"error": "action must be approve or reject"})
            return
        decision = "approved" if action == "approve" else "rejected"
        try:
            import engine

            result = engine.decide_target(tid, decision, reason="대시보드에서 사용자가 결정")
        except KeyError:
            self._reply(404, {"error": "unknown target: " + tid})
            return
        except Exception as error:  # noqa: BLE001 - 대시보드에 원인을 그대로 보여준다
            self._reply(500, {"error": str(error)})
            return
        self._reply(200, {"ok": True, "result": result})

    def _reply(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _state(self) -> dict[str, object]:
        map_data = self._read_map()
        event_data = self._read_events()
        ledger_data = self._read_ledger()
        state_data = self._read_state()
        latest = max(
            float(map_data["mtime"]),
            float(event_data["mtime"]),
            float(ledger_data["mtime"]),
            float(state_data["mtime"]),
        )
        return {
            **map_data,
            **event_data,
            **ledger_data,
            **state_data,
            "updated": datetime.fromtimestamp(latest).strftime("%H:%M:%S") if latest else None,
        }

    def _read_state(self) -> dict[str, object]:
        empty: dict[str, object] = {
            "targets": {},
            "current_stage": None,
            "state_mtime_ns": 0,
            "state_size": 0,
            "mtime": 0.0,
        }
        try:
            stat = self.state_path.stat()
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(value, dict):
            return empty
        targets = value.get("targets")
        return {
            "targets": targets if isinstance(targets, dict) else {},
            "current_stage": value.get("current_stage"),
            "state_mtime_ns": stat.st_mtime_ns,
            "state_size": stat.st_size,
            "mtime": stat.st_mtime,
        }

    def _read_map(self) -> dict[str, object]:
        try:
            stat = self.map_path.stat()
            return {"map_exists": True, "map_path": str(self.map_path), "map_content": self.map_path.read_text(encoding="utf-8", errors="replace"), "map_mtime_ns": stat.st_mtime_ns, "map_size": stat.st_size, "mtime": stat.st_mtime}
        except OSError:
            return {"map_exists": False, "map_path": str(self.map_path), "map_content": "", "map_mtime_ns": 0, "map_size": 0, "mtime": 0.0}

    def _read_events(self) -> dict[str, object]:
        try:
            stat = self.events_path.stat()
            parsed: list[dict[str, object]] = []
            ids: set[str] = set()
            for raw in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(value, dict):
                    continue
                if self.stage_filter and value.get("stage_id") != self.stage_filter:
                    continue
                safe = {key: value.get(key) for key in SAFE_EVENT_FIELDS if key in value}
                parsed.append(safe)
                if isinstance(value.get("event_id"), str):
                    ids.add(value["event_id"])
            return {"events_path": str(self.events_path), "events": parsed[-1000:], "event_count": len(ids), "events_mtime_ns": stat.st_mtime_ns, "events_size": stat.st_size, "mtime": stat.st_mtime}
        except OSError:
            return {"events_path": str(self.events_path), "events": [], "event_count": 0, "events_mtime_ns": 0, "events_size": 0, "mtime": 0.0}

    def _read_ledger(self) -> dict[str, object]:
        try:
            stat = self.ledger_path.stat()
            return {"ledger_path": str(self.ledger_path), "ledger_content": self.ledger_path.read_text(encoding="utf-8", errors="replace"), "ledger_mtime_ns": stat.st_mtime_ns, "ledger_size": stat.st_size, "mtime": stat.st_mtime}
        except OSError:
            return {"ledger_path": str(self.ledger_path), "ledger_content": "", "ledger_mtime_ns": 0, "ledger_size": 0, "mtime": 0.0}

    def log_message(self, format: str, *args: object) -> None:
        return


def available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("사용 가능한 로컬 포트를 찾지 못했습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description="MAP.md + EVENTS.jsonl 실시간 로컬 대시보드")
    parser.add_argument("map_file", nargs="?", default="MAP.md", help="감시할 MAP.md 경로")
    parser.add_argument("--events", help="EVENTS.jsonl 경로; 생략하면 MAP.md와 같은 폴더")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stage", help="표시할 Stage의 이벤트만 필터링")
    parser.add_argument("--label", default="전체", help="페이지 제목에 표시할 이름")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    map_path = Path(args.map_file).expanduser().resolve()
    events_path = Path(args.events).expanduser().resolve() if args.events else map_path.parent / "EVENTS.jsonl"
    ledger_path = map_path.parent / "LEDGER.md"
    state_path = map_path.parent / "runtime" / "STATE.json"
    # 승인 버튼이 engine과 같은 engagement 루트를 쓰도록 맞춘다.
    os.environ.setdefault("REDTEAM_RUN_DIR", str(map_path.parent))
    port = available_port(args.port)
    # 실행마다 새로 만드는 승인 토큰. 페이지에 심어 두고 POST에서 다시 확인한다.
    csrf_token = secrets.token_urlsafe(32)
    page_bytes = (
        PAGE.replace("__DASHBOARD_LABEL__", html.escape(args.label))
        .replace("__CSRF_TOKEN__", csrf_token)
        .encode("utf-8")
    )
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {
            "map_path": map_path,
            "events_path": events_path,
            "ledger_path": ledger_path,
            "state_path": state_path,
            "page_bytes": page_bytes,
            "stage_filter": args.stage,
            "csrf_token": csrf_token,
            "allowed_origins": frozenset(
                {
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                    f"http://[::1]:{port}",
                }
            ),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}"
    print(f"실시간 지도: {url}")
    print(f"MAP: {map_path}")
    print(f"EVENTS: {events_path}")
    print(f"Stage 필터: {args.stage or '전체'}")
    print("종료하려면 이 창에서 Control-C를 누르세요.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
