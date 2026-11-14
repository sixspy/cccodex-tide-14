<script setup>
import { computed, ref } from "vue";

const token = ref(localStorage.getItem("tide.token") || "");
const profile = ref(JSON.parse(localStorage.getItem("tide.profile") || "null"));
const username = ref("admin");
const password = ref("123456");
const loginError = ref("");

const tab = ref("staged");
const batches = ref([]);
const current = ref(null);
const queue = ref([]);
const landedRows = ref([]);
const settings = ref(null);

const title = ref("");
const rawText = ref("");
const formError = ref("");
const busy = ref(false);

const focusId = ref(null);
const cleanMethod = ref("keep");
const cleanError = ref("");
const landError = ref("");
const settingsPolicy = ref("strict");
const settingsNote = ref("");
const settingsError = ref("");
const settingsOk = ref("");

const canWrite = computed(() => profile.value && profile.value.role === "writer");

const waitStaged = computed(() => queue.value.filter((row) => row.status === "待清洗"));
const readyStaged = computed(() => queue.value.filter((row) => row.status === "已放行"));
const pendingCount = computed(() => queue.value.length);

const focusRow = computed(() => queue.value.find((row) => row.id === focusId.value) || null);

const focusEvents = computed(() => {
  if (!current.value || !focusId.value) return [];
  return current.value.events.filter((event) => event.staged_id === focusId.value);
});

const batchStaged = computed(() =>
  current.value ? current.value.staged.filter((row) => row.status !== "已入库") : []
);
const batchQueued = computed(() =>
  current.value ? queue.value.filter((row) => row.batch_id === current.value.batch.id) : []
);
const batchHasWait = computed(() => batchQueued.value.some((row) => row.status === "待清洗"));

const claimStoreKey = () => `tide.claims.${profile.value ? profile.value.username : "anon"}`;

const claimStore = () => {
  try {
    return JSON.parse(localStorage.getItem(claimStoreKey()) || "{}");
  } catch {
    return {};
  }
};

function claimTokenFor(stagedId) {
  const store = claimStore();
  const key = String(stagedId);
  if (!store[key]) {
    store[key] = crypto.randomUUID();
    localStorage.setItem(claimStoreKey(), JSON.stringify(store));
  }
  return store[key];
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token.value) headers.Authorization = `Bearer ${token.value}`;
  const response = await fetch(path, { ...options, headers });
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch {
    data = {};
  }
  if (!response.ok) {
    const message = data.detail || "请求没有完成";
    throw new Error(typeof message === "string" ? message : "请求没有完成");
  }
  return data;
}

async function loadSettings() {
  settings.value = await request("/api/settings");
  if (!settingsNote.value && settings.value.detail_note) {
    settingsNote.value = settings.value.detail_note;
  }
  if (settings.value.policy) settingsPolicy.value = settings.value.policy;
}

async function refresh() {
  await Promise.all([
    request("/api/batches").then((rows) => (batches.value = rows)),
    request("/api/pending").then((data) => {
      queue.value = data.rows;
      if (data.settings) settings.value = { ...settings.value, ...data.settings };
    }),
    loadSettings(),
  ]);
  if (!current.value && batches.value.length) {
    await openBatch(batches.value[0].id);
  } else if (current.value) {
    // 刷新时留住正在看的那一行，处置提示才不会跳走。
    await openBatch(current.value.batch.id, true);
  }
  if (focusId.value && !focusRow.value) focusId.value = null;
}

async function openBatch(id, keepFocus = false) {
  current.value = await request(`/api/batches/${id}`);
  if (!keepFocus) focusId.value = null;
}

async function selectQueued(row) {
  focusId.value = row.id;
  cleanError.value = "";
  if (!current.value || current.value.batch.id !== row.batch_id) {
    await openBatch(row.batch_id, true);
  }
}

async function submitLogin() {
  loginError.value = "";
  try {
    const data = await request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value, password: password.value }),
    });
    token.value = data.access_token;
    profile.value = { username: data.username, role: data.role, title: data.title };
    localStorage.setItem("tide.token", token.value);
    localStorage.setItem("tide.profile", JSON.stringify(profile.value));
    await refresh();
  } catch (error) {
    loginError.value = error.message;
  }
}

async function submitBatch() {
  formError.value = "";
  busy.value = true;
  try {
    const data = await request("/api/batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.value, raw_text: rawText.value }),
    });
    title.value = "";
    rawText.value = "";
    tab.value = "staged";
    await refresh();
    current.value = data;
    focusId.value = null;
  } catch (error) {
    formError.value = error.message;
  } finally {
    busy.value = false;
  }
}

async function cleanFocused(method) {
  cleanError.value = "";
  if (!focusRow.value) return;
  try {
    await request(`/api/staged/${focusRow.value.id}/clean`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        method,
        claim_token: claimTokenFor(focusRow.value.id),
      }),
    });
    const keep = focusRow.value.batch_id;
    await refresh();
    await openBatch(keep, true);
    if (focusId.value && !focusRow.value) focusId.value = null;
  } catch (error) {
    cleanError.value = error.message;
    try {
      await refresh();
    } catch {
      /* 刷新失败时保留原提示 */
    }
  }
}

async function landCurrent() {
  landError.value = "";
  if (!current.value) return;
  try {
    await request(`/api/batches/${current.value.batch.id}/land`, { method: "POST" });
    const id = current.value.batch.id;
    await refresh();
    focusId.value = null;
    await openBatch(id, false);
    await loadLanded();
    tab.value = "landed";
  } catch (error) {
    landError.value = error.message;
  }
}

async function loadLanded() {
  landedRows.value = await request("/api/landed");
}

async function openLandedTab() {
  tab.value = "landed";
  await loadLanded();
}

async function saveSettings() {
  settingsError.value = "";
  settingsOk.value = "";
  try {
    const data = await request("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: settingsPolicy.value, detail_note: settingsNote.value }),
    });
    settings.value = { ...settings.value, ...data, pending_count: queue.value.length };
    settingsOk.value = "策略单已写定，本日不再更换";
    await loadSettings();
  } catch (error) {
    settingsError.value = error.message;
  }
}

function logout() {
  token.value = "";
  profile.value = null;
  current.value = null;
  queue.value = [];
  landedRows.value = [];
  settings.value = null;
  focusId.value = null;
  localStorage.removeItem("tide.token");
  localStorage.removeItem("tide.profile");
}

if (token.value) {
  refresh().catch(() => logout());
}
</script>

<template>
  <header class="top">
    <div>
      <p class="eyebrow">水文组 · 报文归集</p>
      <h1>潮位报文归集台</h1>
    </div>
    <div v-if="profile" class="who">
      <span>{{ profile.title }} {{ profile.username }}</span>
      <button class="ghost" @click="logout">离开</button>
    </div>
  </header>

  <main v-if="!profile" class="gate">
    <form @submit.prevent="submitLogin">
      <p>站码、时刻、潮位厘米、质量旗。一行一条，竖线分开。</p>
      <label>用户<input v-model="username" autocomplete="username" /></label>
      <label>口令<input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>进入归集台</button>
      <p v-if="loginError" class="warn">{{ loginError }}</p>
    </form>
  </main>

  <main v-else class="desk">
    <nav class="tabs">
      <button :class="{ on: tab === 'staged' }" @click="tab = 'staged'">待落</button>
      <button :class="{ on: tab === 'landed' }" @click="openLandedTab">已入库</button>
      <span class="count">
        待落队列 {{ pendingCount }} 行 · 待清洗 {{ waitStaged.length }} · 已放行
        {{ readyStaged.length }}
      </span>
    </nav>

    <section v-if="tab === 'staged'" class="staged-grid">
      <div class="column">
        <section class="paste">
          <h2>贴入这一批</h2>
          <p class="rule">
            报文先解析进待落队列，不直接入库。清洗由单独动作做，入库再由单独动作做。
          </p>
          <label>批次名<input v-model="title" :disabled="!canWrite" placeholder="例如 堡镇 11-13 午班" /></label>
          <textarea
            v-model="rawText"
            :disabled="!canWrite"
            placeholder="BZ03|202611130900|176|A"
            spellcheck="false"
          />
          <button :disabled="!canWrite || busy" @click="submitBatch">解析进待落队列</button>
          <p v-if="!canWrite" class="quiet">档案员只能翻待落与已入库的行。</p>
          <p v-if="formError" class="warn">{{ formError }}</p>
        </section>

        <section class="settings-card">
          <h2>清洗设置</h2>
          <p class="rule">可疑旗在这一处定规：改旗后入库，或原旗入库并在详情保留原旗。</p>
          <p class="note">
            当前策略单：<strong>{{ settings ? settings.policy_label : "载入中" }}</strong>
            <span v-if="settings && settings.detail_note"> · {{ settings.detail_note }}</span>
            <span v-if="settings && settings.policy_date" class="lock"> · 本日已写定</span>
          </p>
          <label>
            清洗方式
            <select v-model="settingsPolicy" :disabled="!canWrite || (settings && settings.policy_date)">
              <option value="strict">严格清洗：可疑旗须改为实测或插补</option>
              <option value="keep_b">留旗入库：可疑旗原旗入库，详情留原旗</option>
            </select>
          </label>
          <label>
            说明
            <input v-model="settingsNote" :disabled="!canWrite || (settings && settings.policy_date)" placeholder="例如 周五晚班可疑旗按留旗" />
          </label>
          <button
            :disabled="!canWrite || pendingCount > 0 || (settings && settings.policy_date) || busy"
            @click="saveSettings"
          >
            写定策略单
          </button>
          <p v-if="pendingCount > 0" class="quiet">待落队列里还有 {{ pendingCount }} 行，清完之前不许改清洗设置。</p>
          <p v-else-if="settings && settings.policy_date" class="quiet">本日策略单已写定，不得更换清洗方式。</p>
          <p v-if="settingsError" class="warn">{{ settingsError }}</p>
          <p v-if="settingsOk" class="ok">{{ settingsOk }}</p>
        </section>
      </div>

      <div class="column">
        <section class="slips">
          <h2>已收批次</h2>
          <button
            v-for="batch in batches"
            :key="batch.id"
            class="slip"
            :class="{ on: current && current.batch.id === batch.id }"
            @click="openBatch(batch.id)"
          >
            <strong>{{ batch.title }}</strong>
            <em>{{ batch.status }}</em>
            <span>待落 {{ batch.staged_count }} 行 · 已入库 {{ batch.landed_count }} 行 · 未接纳 {{ batch.rejected_count }} 行</span>
          </button>
        </section>

        <article v-if="current" class="tape">
          <header>
            <h3>待落列表 · {{ current.batch.title }}</h3>
            <span>{{ current.batch.status }}</span>
          </header>
          <table>
            <thead>
              <tr><th>行</th><th>站</th><th>时刻</th><th>厘米</th><th>旗</th><th>状态</th><th>方式名</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in batchStaged" :key="row.id">
                <td>{{ row.line_no }}</td>
                <td>{{ row.station_code }}</td>
                <td>{{ row.observed_at }}</td>
                <td>{{ row.tide_cm }}</td>
                <td>{{ row.original_label }}</td>
                <td><span class="pill" :class="row.status === '待清洗' ? 'wait' : 'ready'">{{ row.status }}</span></td>
                <td>{{ row.method_label }}</td>
              </tr>
              <tr v-if="!batchStaged.length"><td colspan="7">这一批没有待落的行</td></tr>
            </tbody>
          </table>

          <h4>未接纳</h4>
          <ul>
            <li v-for="row in current.rejected" :key="row.line_no">
              <code>{{ row.raw_line }}</code>
              <span>{{ row.reason }}</span>
            </li>
            <li v-if="!current.rejected.length">没有被挡下的行</li>
          </ul>

          <h4>入库</h4>
          <p class="rule">待落列表全部清洗放行后，才能作为单独动作入库。</p>
          <button
            :disabled="!canWrite || !batchQueued.length || batchHasWait"
            @click="landCurrent"
          >
            入库这一批
          </button>
          <p v-if="landError" class="warn">{{ landError }}</p>
        </article>
      </div>

      <div class="column">
        <section class="queue">
          <h2>待落队列</h2>
          <p class="rule">清一行的方式是先认领再清洗。同一行被两人同时清洗，只留先认领的结果。</p>
          <button
            v-for="row in queue"
            :key="row.id"
            class="queue-row"
            :class="{ on: focusId === row.id }"
            @click="selectQueued(row)"
          >
            <span class="queue-main">
              <strong>{{ row.station_code }} · {{ row.observed_at }}</strong>
              <em>{{ row.tide_cm }} cm · 原旗 {{ row.original_label }}</em>
            </span>
            <span class="queue-side">
              <span class="pill" :class="row.status === '待清洗' ? 'wait' : 'ready'">{{ row.status }}</span>
              <small>{{ row.method_label }}</small>
            </span>
          </button>
          <p v-if="!queue.length" class="quiet">待落队列已清空。</p>
        </section>

        <section class="detail" v-if="focusRow">
          <h2>清洗详情</h2>
          <p class="rule">{{ focusRow.batch_title }} · 第 {{ focusRow.line_no }} 行 · {{ focusRow.station_code }}</p>
          <div class="compare">
            <div>
              <small>清洗前</small>
              <strong>{{ focusRow.original_label }}（{{ focusRow.original_flag }}）</strong>
            </div>
            <span class="arrow">→</span>
            <div>
              <small>清洗后</small>
              <strong v-if="focusRow.cleaned_flag">{{ focusRow.cleaned_label }}（{{ focusRow.cleaned_flag }}）</strong>
              <strong v-else class="quiet">尚未清洗</strong>
            </div>
          </div>
          <p class="note">
            方式名：{{ focusRow.method_label }}
            <span v-if="focusRow.cleaned_by"> · {{ focusRow.cleaned_by }} 于 {{ focusRow.cleaned_at }}</span>
          </p>

          <div v-if="focusRow.status === '待清洗'">
            <p class="rule">清洗方式要跟当日策略单一致，偏离会记清洗方式不符。</p>
            <label>
              清洗方式
              <select v-model="cleanMethod" :disabled="!canWrite">
                <option value="keep">原旗放行</option>
                <option value="to_a">改记实测</option>
                <option value="to_m">改记插补</option>
              </select>
            </label>
            <button :disabled="!canWrite || busy" @click="cleanFocused(cleanMethod)">认领并清洗这一行</button>
          </div>
          <p v-else class="quiet">这行已经放行，清洗结果以先认领的为准。</p>
          <p v-if="cleanError" class="warn">{{ cleanError }}</p>

          <h4>本行处置记录</h4>
          <ul>
            <li v-for="event in focusEvents" :key="event.created_at + event.event + event.actor">
              <strong>{{ event.event }}</strong>
              <span>{{ event.actor }} · {{ event.detail }} · {{ event.created_at }}</span>
            </li>
            <li v-if="!focusEvents.length">还没有记录。</li>
          </ul>
        </section>
      </div>
    </section>

    <section v-else class="landed-card">
      <header class="landed-head">
        <h2>已入库</h2>
        <button class="ghost-dark" @click="loadLanded">刷新</button>
      </header>
      <p class="rule">入库与待落分开：这里只列已经落库的行，详情对照清洗前后的旗。</p>
      <table>
        <thead>
          <tr>
            <th>批次</th><th>行</th><th>站</th><th>时刻</th><th>厘米</th>
            <th>清洗前</th><th>入库旗</th><th>方式名</th><th>清洗人</th><th>入库人</th><th>入库时刻</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in landedRows" :key="row.id">
            <td>{{ row.batch_title }}</td>
            <td>{{ row.line_no }}</td>
            <td>{{ row.station_code }}</td>
            <td>{{ row.observed_at }}</td>
            <td>{{ row.tide_cm }}</td>
            <td>{{ row.original_label }}（{{ row.original_flag }}）</td>
            <td>{{ row.flag_label }}（{{ row.quality_flag }}）</td>
            <td>{{ row.method_label }}</td>
            <td>{{ row.cleaned_by }}</td>
            <td>{{ row.landed_by }}</td>
            <td>{{ row.landed_at }}</td>
          </tr>
          <tr v-if="!landedRows.length"><td colspan="11">还没有入库的行</td></tr>
        </tbody>
      </table>
    </section>
  </main>
</template>

<style scoped>
.top, .desk, .gate { width: min(1400px, calc(100% - 32px)); margin: 0 auto; }
.top { display: flex; justify-content: space-between; align-items: end; padding: 28px 0 12px; }
.eyebrow { letter-spacing: 0.18em; text-transform: uppercase; color: #d7a15f; font-size: 12px; margin: 0 0 6px; }
h1 { margin: 0; font-weight: 560; font-size: 34px; }
h2, h3, h4 { margin: 0; }
.who { display: flex; gap: 12px; align-items: center; }
.ghost { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.35); padding: 6px 12px; }
.ghost-dark { background: transparent; color: #9d2c2c; border: 1px solid #d9d0bc; padding: 6px 12px; }
.gate { padding: 24px 0 48px; }
.gate form, .paste, .settings-card, .tape, .queue, .detail, .landed-card {
  background: #f3efe2;
  color: #1c2430;
  padding: 22px;
}
.gate form { width: min(420px, 100%); display: grid; gap: 12px; }
label { display: grid; gap: 6px; font-size: 14px; }
input, textarea, select {
  width: 100%;
  border: 0;
  background: #fffdf8;
  color: #1c2430;
  padding: 10px 12px;
  box-shadow: inset 0 0 0 1px #d9d0bc;
}
textarea { min-height: 160px; font-family: "IBM Plex Mono", Consolas, monospace; line-height: 1.6; }
button { background: #9d2c2c; color: #fffaf2; padding: 10px 14px; }
.tabs { display: flex; gap: 8px; align-items: center; padding: 4px 0 18px; }
.tabs button { background: transparent; color: #f3efe2; border: 1px solid rgba(243, 239, 226, 0.3); }
.tabs button.on { background: #d7a15f; color: #142033; border-color: #d7a15f; }
.tabs .count { color: #a9b2c0; font-size: 13px; margin-left: 6px; }
.staged-grid { display: grid; grid-template-columns: 330px minmax(0, 1fr) 340px; gap: 18px; align-items: start; padding-bottom: 48px; }
.column { display: grid; gap: 18px; align-content: start; }
.paste, .settings-card, .queue, .detail { display: grid; gap: 12px; align-content: start; }
.rule, .quiet { margin: 0; color: #5c5648; font-size: 13px; }
.note { margin: 0; color: #5c5648; font-size: 13px; }
.lock { color: #9d2c2c; }
.ok { color: #2c6b4f; margin: 0; font-size: 13px; }
.slip {
  text-align: left;
  background: transparent;
  color: #f3efe2;
  border: 1px solid rgba(243, 239, 226, 0.2);
  padding: 12px 14px;
  display: grid;
  gap: 4px;
  margin-bottom: 8px;
}
.slip.on { border-color: #d7a15f; }
.slip em { font-style: normal; color: #d7a15f; }
.slip span, .tape li span, .queue small { color: #5c5648; }
.queue-row {
  text-align: left;
  background: #fffdf8;
  color: #1c2430;
  border: 1px solid #d9d0bc;
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.queue-row.on { border-color: #9d2c2c; box-shadow: inset 3px 0 0 #9d2c2c; }
.queue-main { display: grid; gap: 2px; }
.queue-main em { font-style: normal; font-size: 13px; color: #5c5648; }
.queue-side { display: grid; gap: 4px; justify-items: end; font-size: 12px; }
.pill { padding: 2px 8px; font-size: 12px; border-radius: 2px; background: #e6ddc8; color: #1c2430; white-space: nowrap; }
.pill.wait { background: #e8c9c9; color: #7c2020; }
.pill.ready { background: #cfe3d6; color: #255c42; }
.compare { display: flex; align-items: center; gap: 10px; }
.compare > div { display: grid; gap: 2px; }
.compare small { color: #5c5648; }
.compare .arrow { color: #9d2c2c; }
.tape header, .landed-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
.tape h4 { margin: 14px 0 8px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid #e4dcc8; font-size: 13px; vertical-align: top; }
ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; gap: 2px; }
code { font-family: Consolas, monospace; }
.warn { color: #9d2c2c; margin: 0; font-size: 13px; }
.landed-card { display: grid; gap: 10px; margin-bottom: 48px; overflow-x: auto; }
@media (max-width: 1100px) {
  .staged-grid { grid-template-columns: 1fr; }
}
</style>
