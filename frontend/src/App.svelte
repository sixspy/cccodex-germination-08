<script>
  let profile = null;
  let username = "admin";
  let password = "123456";
  let loginError = "";
  let trials = [];
  let detail = null;
  let formError = "";
  let notice = "";
  let busy = false;

  try {
    profile = JSON.parse(localStorage.getItem("germ.profile") || "null");
  } catch {
    profile = null;
  }

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem("germ.token") || "";
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(data.detail || "请求没有完成"), { status: response.status });
    return data;
  }

  async function refresh(prefer) {
    trials = await request("/api/trials");
    const target = prefer || detail?.trial.id || trials[0]?.id;
    if (target) detail = await request(`/api/trials/${target}`);
  }

  async function submitLogin() {
    loginError = "";
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("germ.token", data.access_token);
      profile = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("germ.profile", JSON.stringify(profile));
      await refresh();
    } catch (error) {
      loginError = error.message;
    }
  }

  async function openTrial(trial) {
    formError = "";
    notice = "";
    detail = await request(`/api/trials/${trial.id}`);
  }

  async function publishCurrent() {
    if (!detail) return;
    busy = true;
    formError = "";
    notice = "";
    try {
      const data = await request(`/api/trials/${detail.trial.id}/publish`, { method: "POST" });
      notice = data.skipped
        ? `${data.detail}（第 ${data.skipped} 次跳过，台账不重复入册）`
        : data.detail;
      detail = data;
      trials = await request("/api/trials");
    } catch (error) {
      // 口径验收失败或争用：后端已整笔回滚，前端只如实展示，不做本地修补。
      formError = error.status === 409 ? `${error.message}（后手登记未写入，请重试）` : error.message;
    } finally {
      busy = false;
    }
  }

  async function rollback(keepValid) {
    if (!detail) return;
    const verb = keepValid ? "仅保留合格记录" : "整笔撤回";
    if (!window.confirm(`确认对批次 ${detail.trial.lot_code} 执行「${verb}」？`)) return;
    busy = true;
    formError = "";
    notice = "";
    try {
      const data = await request(`/api/trials/${detail.trial.id}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keep_valid: keepValid }),
      });
      notice = data.detail;
      detail = data;
      trials = await request("/api/trials");
    } catch (error) {
      formError = error.status === 409 ? `${error.message}（争用未处理，请重试）` : error.message;
    } finally {
      busy = false;
    }
  }

  function logout() {
    localStorage.removeItem("germ.token");
    localStorage.removeItem("germ.profile");
    profile = null;
    detail = null;
  }

  if (profile) {
    refresh().catch(() => logout());
  }
</script>

{#if !profile}
  <main class="paper gate">
    <p class="kicker">种子检验室</p>
    <h1>发芽口径核算台</h1>
    <p>现行口径：分母是播种粒数减去霉粒数，分子是发芽粒数。霉变种子不计入可计种子。</p>
    <form on:submit|preventDefault={submitLogin}>
      <label>用户<input bind:value={username} /></label>
      <label>口令<input type="password" bind:value={password} /></label>
      <button>打开记录</button>
      {#if loginError}<p class="warn">{loginError}</p>{/if}
    </form>
  </main>
{:else}
  <header class="bar">
    <div>
      <p class="kicker">口径核算</p>
      <h1>发芽口径核算台</h1>
    </div>
    <div class="who">
      <span>{profile.title} {profile.username}</span>
      <button class="quiet" on:click={logout}>离开</button>
    </div>
  </header>
  <main class="desk">
    <aside>
      <p class="aside-title">批次列表 · 批次号可核对</p>
      {#each trials as trial}
        <button class:on={detail && detail.trial.id === trial.id} on:click={() => openTrial(trial)}>
          <strong class="lot-code">{trial.lot_code}</strong>
          <em>{trial.crop_name}</em>
          <span class:done={trial.status === "已按现行口径出数"}>{trial.status}</span>
        </button>
      {/each}
    </aside>
    {#if detail}
      <article class="paper">
        <div class="head">
          <h2><span class="lot-code">{detail.trial.lot_code}</span> · {detail.trial.crop_name}</h2>
          <button disabled={profile.role !== "writer" || busy} on:click={publishCurrent}>按现行口径出数</button>
        </div>
        <p class="status-line">台账状态：{detail.trial.status}</p>
        <p class="rule">现行口径的分母是播种粒数减去霉粒数。旧口径把霉粒留在分母里，出过的数字不会被抹掉。归档按整批统一裁定，验收不过整笔不入册。</p>
        {#if profile.role !== "writer"}<p>见证员只能核对，不能出数或撤回。</p>{/if}
        {#if notice}<p class="notice">{notice}</p>{/if}
        {#if formError}<p class="warn">{formError}</p>{/if}
        <table>
          <thead>
            <tr><th>床</th><th>播种</th><th>发芽</th><th>霉粒</th></tr>
          </thead>
          <tbody>
            {#each detail.beds as bed}
              <tr>
                <td>{bed.bed_no}</td>
                <td>{bed.sown}</td>
                <td>{bed.sprouted}</td>
                <td>{bed.moldy}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="ledger-head">
          <h3>出数记录（台账）</h3>
          {#if profile.role === "writer"}
            <span class="rollback-tools">
              <button class="outline" disabled={busy || detail.publications.length === 0} on:click={() => rollback(false)}>整笔撤回</button>
              <button class="outline" disabled={busy || detail.publications.length === 0} on:click={() => rollback(true)}>仅保留合格</button>
            </span>
          {/if}
        </div>
        {#if detail.publications.length === 0}
          <p class="rule">台账已空，没有可对读的出数条目。</p>
        {/if}
        {#each detail.publications as row}
          <section class="edition" class:old={!row.current}>
            <div class="edition-top">
              <strong>{row.current ? "现行口径" : "旧口径"} {row.rate_label}</strong>
              <span class="lot-tag">{detail.trial.lot_code}</span>
            </div>
            <span>分母 {row.tested} · 剔除霉粒 {row.excluded_mold} · 发芽 {row.sprouted}</span>
            <em>{row.note} · {row.published_by} · {row.published_at}</em>
          </section>
        {/each}
      </article>
    {/if}
  </main>
{/if}

<style>
  .bar, .desk, .gate { width: min(1100px, calc(100% - 32px)); margin: 0 auto; }
  .bar { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 8px; }
  .kicker { margin: 0 0 4px; letter-spacing: 0.16em; color: #b4332a; font-size: 13px; }
  h1, h2, h3 { font-weight: 560; margin: 0; }
  .who { display: flex; gap: 12px; align-items: center; }
  .desk { display: grid; grid-template-columns: 260px 1fr; gap: 18px; padding-bottom: 40px; }
  .paper { background: rgba(255, 250, 242, 0.94); padding: 22px; box-shadow: 0 10px 30px rgba(44, 36, 22, 0.05); }
  .gate { margin-top: 40px; width: min(520px, calc(100% - 32px)); }
  form, aside { display: grid; gap: 10px; }
  label { display: grid; gap: 6px; }
  input { border: 0; border-bottom: 1px solid #b4332a; background: transparent; padding: 8px 0; color: #2c2416; }
  button { background: #b4332a; color: #fffaf2; border: 0; padding: 8px 12px; }
  button.outline { background: transparent; color: #b4332a; border: 1px solid #b4332a; padding: 6px 10px; }
  .quiet { background: transparent; color: #2c2416; border-bottom: 1px solid #2c2416; }
  .aside-title { font-size: 13px; color: #7a6a55; margin: 0 0 2px; }
  aside button { text-align: left; background: transparent; color: #2c2416; border-bottom: 1px solid rgba(180, 51, 42, 0.35); display: grid; gap: 2px; padding: 10px 8px; }
  aside button.on { background: #fffaf2; outline: 1px solid rgba(180, 51, 42, 0.45); }
  aside em, .edition em { font-style: normal; color: #7a6a55; }
  .lot-code { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; letter-spacing: 0.04em; }
  .lot-tag { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: #b4332a; border: 1px solid rgba(180, 51, 42, 0.5); padding: 1px 6px; }
  .head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
  .status-line { margin: 8px 0 4px; font-weight: 560; }
  .rule { color: #7a6a55; }
  .notice { color: #1d6b3a; background: rgba(29, 107, 58, 0.08); padding: 8px 10px; }
  .ledger-head { display: flex; justify-content: space-between; align-items: center; margin: 12px 0 4px; }
  .rollback-tools { display: flex; gap: 8px; }
  table { width: 100%; border-collapse: collapse; margin: 8px 0 16px; }
  th, td { text-align: left; padding: 8px 4px; border-bottom: 1px solid rgba(180, 51, 42, 0.25); }
  .edition { display: grid; gap: 4px; padding: 10px 0; border-top: 1px dashed rgba(180, 51, 42, 0.4); }
  .edition-top { display: flex; justify-content: space-between; align-items: center; }
  .old strong { color: #7a6a55; }
  .warn { color: #b4332a; }
  @media (max-width: 800px) {
    .desk { grid-template-columns: 1fr; }
  }
</style>
