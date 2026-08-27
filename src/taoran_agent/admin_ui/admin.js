(() => {
  "use strict";

  let adminKey = "";
  let authorizedApplications = [];
  let connectedApiKey = "";
  const $ = (selector) => document.querySelector(selector);
  const loginPanel = $("#loginPanel");
  const workspace = $("#workspace");
  const loginMessage = $("#loginMessage");
  const connectionMessage = $("#connectionMessage");
  const saveMessage = $("#saveMessage");
  const formConfiguration = $("#formConfiguration");
  const apiKeyInput = document.querySelector('[name="jiandaoyun_api_key"]');

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

  function resetAuthorization() {
    authorizedApplications = [];
    connectedApiKey = "";
    formConfiguration.classList.add("hidden");
    $("#applicationSelect").replaceChildren();
    $("#formSelect").replaceChildren();
    clearMessage(connectionMessage);
  }

  async function loadTenants() {
    const data = await request("/api/v1/admin/tenants");
    const list = $("#tenantList");
    if (!data.tenants.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "还没有客户，可以从上方开始第一次接入。";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...data.tenants.map((tenant) => {
      const row = document.createElement("div");
      row.className = "tenant-row";
      const name = document.createElement("strong");
      name.textContent = tenant.display_name;
      const id = document.createElement("span");
      id.textContent = `系统编号：${tenant.tenant_id}`;
      const form = document.createElement("span");
      form.textContent = tenant.jiandaoyun.entry_name || "表单待确认";
      const state = document.createElement("span");
      state.className = `pill ${tenant.enabled ? "on" : "off"}`;
      state.textContent = tenant.enabled ? "已启用" : "未启用";
      row.append(name, id, form, state);
      return row;
    }));
  }

  function populateForms() {
    const applicationId = $("#applicationSelect").value;
    const application = authorizedApplications.find((item) => item.app_id === applicationId);
    const formSelect = $("#formSelect");
    formSelect.replaceChildren();
    (application?.forms || []).forEach((form) => {
      const option = document.createElement("option");
      option.value = form.entry_id;
      option.textContent = form.name;
      formSelect.append(option);
    });
  }

  function populateApplications() {
    const applicationSelect = $("#applicationSelect");
    applicationSelect.replaceChildren();
    authorizedApplications.forEach((application) => {
      const option = document.createElement("option");
      option.value = application.app_id;
      option.textContent = application.name;
      applicationSelect.append(option);
    });
    populateForms();
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
    resetAuthorization();
    apiKeyInput.value = "";
    workspace.classList.add("hidden");
    loginPanel.classList.remove("hidden");
    $("#serverStatus").textContent = "未连接";
    $("#serverStatus").className = "status muted";
  });

  $("#refreshButton").addEventListener("click", async () => {
    try { await loadTenants(); } catch (error) { showMessage(saveMessage, error.message); }
  });

  apiKeyInput.addEventListener("input", () => {
    if (connectedApiKey && apiKeyInput.value.trim() !== connectedApiKey) {
      resetAuthorization();
    }
  });

  $("#applicationSelect").addEventListener("change", populateForms);

  $("#connectButton").addEventListener("click", async () => {
    clearMessage(connectionMessage);
    clearMessage(saveMessage);
    const apiKey = apiKeyInput.value.trim();
    if (!apiKey) {
      showMessage(connectionMessage, "请先填写简道云 API Key。");
      return;
    }
    const button = $("#connectButton");
    button.disabled = true;
    button.textContent = "正在读取授权应用和表单…";
    formConfiguration.classList.add("hidden");
    try {
      const result = await request("/api/v1/admin/jiandaoyun/authorization", {
        method: "POST",
        body: JSON.stringify({ jiandaoyun_api_key: apiKey }),
      });
      authorizedApplications = (result.applications || []).filter((application) => application.forms?.length);
      if (!authorizedApplications.length) {
        throw new Error("该 API Key 下没有可用表单，请先在简道云中为其授权应用。");
      }
      connectedApiKey = apiKey;
      populateApplications();
      formConfiguration.classList.remove("hidden");
      const formCount = authorizedApplications.reduce((total, application) => total + application.forms.length, 0);
      showMessage(connectionMessage, `连接成功，已读取 ${authorizedApplications.length} 个应用、${formCount} 个表单。`, true);
    } catch (error) {
      resetAuthorization();
      showMessage(connectionMessage, error.message);
    } finally {
      button.disabled = false;
      button.textContent = "连接简道云";
    }
  });

  $("#tenantForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const onboardingForm = event.currentTarget;
    clearMessage(saveMessage);
    if (!connectedApiKey || !authorizedApplications.length) {
      showMessage(saveMessage, "请先连接简道云并读取授权表单。");
      return;
    }
    const applicationId = $("#applicationSelect").value;
    const entryId = $("#formSelect").value;
    const application = authorizedApplications.find((item) => item.app_id === applicationId);
    const selectedForm = application?.forms.find((item) => item.entry_id === entryId);
    if (!application || !selectedForm) {
      showMessage(saveMessage, "请选择要接入的授权应用和表单。");
      return;
    }
    const button = $("#saveButton");
    const payload = {
      tenant_id: null,
      display_name: onboardingForm.elements.display_name.value.trim(),
      application_id: application.app_id,
      entry_id: selectedForm.entry_id,
      entry_name: selectedForm.name,
      jiandaoyun_api_key: connectedApiKey,
      test_connection: true,
      enabled: true,
      rotate_access_key: false,
      rotate_webhook_secret: false,
    };
    button.disabled = true;
    button.textContent = "正在匹配字段并生成配置…";
    try {
      const result = await request("/api/v1/admin/tenants", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderResult(result);
      const stateText = result.activated ? "配置已写入并立即生效" : "配置已保存，请根据提示确认未匹配字段";
      showMessage(saveMessage, `${stateText}，系统客户编号：${result.tenant.tenant_id}`, true);
      apiKeyInput.value = "";
      onboardingForm.elements.display_name.value = "";
      resetAuthorization();
      await loadTenants();
    } catch (error) {
      showMessage(saveMessage, error.message);
    } finally {
      button.disabled = false;
      button.textContent = "完成配置";
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

    const identifier = document.createElement("p");
    identifier.className = "generated-id";
    identifier.textContent = `系统客户编号：${result.tenant.tenant_id}`;
    target.append(identifier);

    const metrics = document.createElement("div");
    metrics.className = "metrics";
    [["已匹配字段", result.mapping_report.matched_count || 0], ["待确认字段", result.mapping_report.unresolved_count || 0]].forEach(([label, value]) => {
      const metric = document.createElement("div");
      metric.className = "metric";
      const caption = document.createElement("span");
      caption.textContent = label;
      const number = document.createElement("strong");
      number.textContent = String(value);
      metric.append(caption, number);
      metrics.append(metric);
    });
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
