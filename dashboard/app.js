(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { base: location.origin, token: "", connected: false, integrations: [] };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const short = (value, n=110) => { const s=String(value ?? ""); return s.length>n ? s.slice(0,n)+"…" : s; };
  const date = (value) => value ? new Date(value).toLocaleString() : "—";
  const statusClass = (s) => ["completed","passed","approved","promoted"].includes(String(s).toLowerCase()) ? "good" : ["failed","blocked","rejected","error"].includes(String(s).toLowerCase()) ? "bad" : "warn";
  function notice(message, bad=false) { const el=$("notice"); el.textContent=message; el.classList.remove("hidden"); el.style.borderColor=bad?"#78434a":"#6a5431"; el.style.background=bad?"#321f26":"#302817"; }
  function clearNotice(){ $("notice").classList.add("hidden"); }
  async function api(path, options={}) {
    const response = await fetch(state.base.replace(/\/$/,"")+path, { ...options, headers: { "Authorization":"Bearer "+state.token, ...(options.body?{"Content-Type":"application/json"}:{}), ...(options.headers||{}) } });
    const data = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(data.detail || ("API request failed ("+response.status+")"));
    return data;
  }
  function setStat(id,n){ $(id).textContent=n; }
  async function refresh() {
    if(!state.connected) return notice("Connect with your BCS API address and bearer token to load live workspace data.");
    clearNotice();
    const results = await Promise.allSettled([
      api("/v1/tasks"),api("/v1/execution-jobs"),api("/v1/approvals"),
      api("/v1/integrations"),api("/v1/integration-promotions")
    ]);
    const [tasks,jobs,approvals,integrations,promotions]=results;
    const failed=results.filter(r=>r.status==="rejected");
    if(failed.length) notice(failed.map(r=>r.reason.message).join(" · "),true);
    const t=tasks.status==="fulfilled"?tasks.value.tasks||[]:[];
    const j=jobs.status==="fulfilled"?jobs.value.jobs||[]:[];
    const a=approvals.status==="fulfilled"?approvals.value.approvals||[]:[];
    const i=integrations.status==="fulfilled"?integrations.value.integrations||[]:[];
    const p=promotions.status==="fulfilled"?promotions.value.promotion_requests||[]:[];
    state.integrations=i;
    setStat("stat-tasks",t.length); setStat("stat-jobs",j.filter(x=>["queued","running"].includes(x.state)).length);
    setStat("stat-approvals",a.filter(x=>x.status==="pending").length); setStat("stat-integrations",i.length);
    $("tasks-body").innerHTML=t.length?t.map(x=>'<tr><td><strong>'+esc(short(x.task.objective,100))+'</strong><div class="item-meta">'+esc(x.task.task_id)+'</div></td><td>'+esc(x.assigned_worker_id||"Unassigned")+'</td><td><span class="status '+statusClass(x.state)+'">'+esc(x.state)+'</span></td><td>'+esc(date(x.updated_at))+'</td></tr>').join(""):'<tr><td colspan="4" class="empty">No tasks yet. Start with a build instruction above.</td></tr>';
    $("jobs-list").innerHTML=j.length?j.map(x=>'<article class="item"><div class="item-main"><div class="item-title">'+esc(x.job_type||"tasks")+' · '+esc(x.job_id)+'</div><div class="item-meta">Tasks: '+esc((x.task_ids||[]).join(", "))+'<br>Updated: '+esc(date(x.updated_at||x.created_at))+(x.error?'<br>Issue: '+esc(x.error):"")+'</div></div><span class="status '+statusClass(x.state)+'">'+esc(x.state)+'</span></article>').join(""):'<p class="empty">No execution jobs yet.</p>';
    $("approvals-list").innerHTML=a.length?a.map(x=>'<article class="item"><div class="item-main"><div class="item-title">'+esc(x.capability)+' · '+esc(x.request_id)+'</div><div class="item-meta">Task: '+esc(x.task_id)+' · Worker: '+esc(x.worker_id)+'<br>Created: '+esc(date(x.created_at))+' · Expires: '+esc(date(x.expires_at))+(x.reason?'<br>'+esc(x.reason):"")+'</div></div><div class="item-actions"><span class="status '+statusClass(x.status)+'">'+esc(x.status)+'</span>'+(x.status==="pending"?'<button class="button primary" data-approval="'+esc(x.request_id)+'" data-decision="true">Approve</button><button class="button" data-approval="'+esc(x.request_id)+'" data-decision="false">Reject</button>':"")+'</div></article>').join(""):'<p class="empty">No approval requests.</p>';
    $("integrations-list").innerHTML=i.length?i.map(x=>'<article class="item"><div class="item-main"><div class="item-title">'+esc(x.integration_id)+' · '+esc(x.branch||"integration branch")+'</div><div class="item-meta">Commit: '+esc(x.commit_sha||"—")+'<br>Workspace groups: '+esc((x.included_groups||[]).join(", "))+'<br>'+esc(x.reason||"")+'</div></div><div class="item-actions"><span class="status '+statusClass(x.status)+'">'+esc(x.status)+'</span>'+(x.status==="awaiting_qa"?'<button class="button" data-qa="'+esc(x.integration_id)+'">Run QA…</button><button class="button" data-promote="'+esc(x.integration_id)+'">Request promotion</button>':"")+'</div></article>').join(""):'<p class="empty">No integration branches yet.</p>';
    $("promotions-list").innerHTML=p.length?p.map(x=>'<article class="item"><div class="item-main"><div class="item-title">'+esc(x.request_id)+' · '+esc(x.integration_id)+'</div><div class="item-meta">Commit: '+esc(x.commit_sha)+' · Target: '+esc(x.target_branch)+'<br>QA run: '+esc(x.qa_run_id)+' · Approver: '+esc(x.promoted_by||"Pending")+'</div></div><div class="item-actions"><span class="status '+statusClass(x.status)+'">'+esc(x.status)+'</span>'+(x.status==="pending"?'<button class="button primary" data-execute-promotion="'+esc(x.request_id)+'">Execute approved promotion</button>':"")+'</div></article>').join(""):'<p class="empty">No promotion requests yet.</p>';
    $("last-updated").textContent="Updated "+new Date().toLocaleTimeString();
  }
  $("connect-form").addEventListener("submit",async e=>{e.preventDefault();state.base=$("api-url").value.trim()||location.origin;state.token=$("api-token").value.trim();try{await api("/health");state.connected=true;$("connection-panel").classList.add("hidden");notice("Connected. Live BCS workspace data loaded.");await refresh();}catch(err){state.connected=false;notice(err.message,true);}});
  $("refresh").addEventListener("click",()=>refresh().catch(e=>notice(e.message,true)));
  $("build-form").addEventListener("submit",async e=>{e.preventDefault();if(!state.connected)return notice("Connect to the BCS API first.",true);const btn=$("build-submit");btn.disabled=true;btn.textContent="BRAIN is planning…";try{const criteria=$("criteria").value.split("\n").map(x=>x.trim()).filter(Boolean);const data=await api("/v1/builds",{method:"POST",body:JSON.stringify({objective:$("objective").value.trim(),acceptance_criteria:criteria})});const steps=data.plan?.steps||[];$("build-result").innerHTML='<strong>Build accepted and queued.</strong><br>Job: '+esc(data.job?.job_id)+'<br>'+steps.length+' specialist steps planned: '+steps.map(s=>esc(s.role)).join(" → ")+'<br>Tasks: '+esc((data.task_ids||[]).join(", "))+'<br>Status: '+esc(data.status_url);$("build-result").classList.remove("hidden");$("objective").value="";$("criteria").value="";await refresh();}catch(err){notice(err.message,true);}finally{btn.disabled=false;btn.textContent="✳ Plan & start build";}});
  document.addEventListener("click",async e=>{const b=e.target.closest("button");if(!b)return;try{
    if(b.dataset.approval){const reason=prompt("Optional reason for this decision:")||"";await api("/v1/approvals/"+encodeURIComponent(b.dataset.approval)+"/decision",{method:"POST",body:JSON.stringify({approved:b.dataset.decision==="true",reason})});notice("Approval decision recorded.");await refresh();}
    if(b.dataset.qa){const profile=prompt("Enter the server-configured QA profile ID:");if(!profile)return;const result=await api("/v1/integrations/"+encodeURIComponent(b.dataset.qa)+"/qa",{method:"POST",body:JSON.stringify({profile_id:profile})});notice("QA queued. Job: "+result.job.job_id);await refresh();}
    if(b.dataset.promote){const result=await api("/v1/integrations/"+encodeURIComponent(b.dataset.promote)+"/promotion-requests",{method:"POST"});notice("Promotion approval request created: "+result.approval.request_id);await refresh();}
    if(b.dataset.executePromotion){if(!confirm("Execute this already-approved fast-forward to development? main will not be changed."))return;await api("/v1/integration-promotions/"+encodeURIComponent(b.dataset.executePromotion)+"/execute",{method:"POST"});notice("Development promotion completed.");await refresh();}
  }catch(err){notice(err.message,true);}});
  $("api-url").value=location.origin;
  refresh();
})();