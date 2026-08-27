(() => {
  "use strict";

  let adminKey = "";
  const $ = (selector) => document.querySelector(selector);
  const loginPanel = $("#loginPanel");
  const workspace = $("#workspace");
  const loginMessage = $("#loginMessage");
  const saveMessage = $("#saveMessage");

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Key": adminKey,
        ...(options.headers || {}),
      },
      cache: "no-store",
    });
    let body = {};
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).join("；")
        : (body.detail || `请求失败（${response.status}）`);
      throw new Error(detail);
    }
    return body;
  }

  function showMessage(element, text, success = false) {
    element.textContent = text;
    element.classList.toggle("success", success);
    element.classList.remove("hidden");
  }

  function clearMessage(element) {
    element.textContent = "";
    element.classList.add("hidden");
  }

  async function loadTenants() {
    const data = await request("/api/v1/admin/tenants");
    const list = $("#tenantList");
    if (!data.tenants.length) {
      list.innerHTML = '<div class="empty">还没有客户，可以从上方开始第一次接入。</div>';
      return;
    }
    list.replaceChildren(...data.tenants.map((tenant) => {
      const row = document.createElement("div");
      row.className = "tenant-row";
      const name = document.createElement("strong");
      name.textContent = tenant.display_name;
      const id = document.createElement("span");
      id.textContent = tenant.tenant_id;
      const form = document.createElement("span");
      form.textContent = tenant.jiandaoyun.entry_name || "表单待确认";
      const state = document.createElement("span");
      state.className = `pill ${tenant.enabled ? "on" : "off"}`;
      state.textContent = tenant.enabled ? "已启用" : "未启用";
      row.append(name, id, form, state);
      return row;
    }));
  }

  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessage(loginMessage);
    adminKey = $("#adminKey").value.trim();
    try {
      const status = await request("/api/v1/admin/status");
      $("#adminKey").value = "";
      $("#serverStatus").textContent = `已连接 · v${status.version}`;
      $("#serverStatus").className = "status ok";
      loginPanel.classList.add("hidden");
      workspace.classList.remove("hidden");
      await loadTenants();
    } catch (error) {
      adminKey = "";
      showMessage(loginMessage, error.message);
    }
  });

  $("#logoutButton").addEventListener("click", () => {
    adminKey = "";
    workspace.classList.add("hidden");
    loginPanel.classList.remove("hidden");
    $("#serverStatus").textContent = "未连接";
    $("#serverStatus").className = "status muted";
  });

  $("#refreshButton").addEventListener("click", async () => {
    try { await loadTenants(); } catch (error) { showMessage(saveMessage, error.message); }
  });

  $("#tenantForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessage(saveMessage);
    const form = event.currentTarget;
    const button = $("#saveButton");
    const values = new FormData(form);
    const payload = {
      tenant_id: values.get("tenant_id"),
      display_name: values.get("display_name"),
      application_id: values.get("application_id"),
      entry_id: values.get("entry_id"),
      entry_name: values.get("entry_name"),
      jiandaoyun_api_key: values.get("jiandaoyun_api_key") || null,
      test_connection: values.has("test_connection"),
      enabled: values.has("enabled"),
      rotate_access_key: values.has("rotate_access_key"),
      rotate_webhook_secret: values.has("rotate_webhook_secret"),
    };
    button.disabled = true;
    button.textContent = payload.test_connection ? "正在连接简道云并匹配字段…" : "正在保存…";
    try {
      const result = await request("/api/v1/admin/tenants", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderResult(result);
      form.elements.jiandaoyun_api_key.value = "";
      showMessage(saveMessage, "配置已写入并立即生效。", true);
      await loadTenants();
    } catch (error) {
      showMessage(saveMessage, error.message);
    } finally {
      button.disabled = false;
      button.textContent = "测试连接并完成配置";
    }
  });

  function renderResult(result) {
    $("#emptyResult").classList.add("hidden");
    const target = $("#resultContent");
    target.classList.remove("hidden");
    target.replaceChildren();

    const badge = document.createElement("div");
    badge.className = `result-badge ${result.activated ? "active" : "pending"}`;
    badge.textContent = result.activated ? "配置成功，客户已启用" : "已保存，暂未启用";
    target.append(badge);

    const metrics = document.createElement("div");
    metrics.className = "metrics";
    metrics.innerHTML = `<div class="metric"><span>已匹配字段</span><strong>${result.mapping_report.matched_count || 0}</strong></div><div class="metric"><span>待确认字段</span><strong>${result.mapping_report.unresolved_count || 0}</strong></div>`;
    target.append(metrics);

    if (result.warnings.length) {
      const warnings = document.createElement("ul");
      warnings.className = "warning-list";
      result.warnings.forEach((warning) => {
        const item = document.createElement("li");
        item.textContent = warning;
        warnings.append(item);
      });
      target.append(warnings);
    }

    const credentials = result.one_time_credentials;
    if (credentials.access_key || credentials.webhook_secret) {
      const box = document.createElement("div");
      box.className = "credential";
      const title = document.createElement("strong");
      title.textContent = "请立即复制，以下密钥只显示这一次";
      box.append(title);
      addCredential(box, "TAORAN 访问 Key", credentials.access_key);
      addCredential(box, "推送签名密钥", credentials.webhook_secret);
      target.append(box);
    }
  }

  function addCredential(parent, label, value) {
    if (!value) return;
    const caption = document.createElement("span");
    caption.textContent = label;
    const code = document.createElement("code");
    code.textContent = value;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "ghost";
    copy.textContent = "复制";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(value);
      copy.textContent = "已复制";
    });
    parent.append(caption, code, copy);
  }
})();
