(() => {
  "use strict";

  let adminKey = "";
  let authorizedApplications = [];
  let connectedApiKey = "";
  let oneTimeCredentials = null;
  let authorizationReady = false;
  let editingTenant = null;
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
    authorizationReady = false;
    editingTenant = null;
    formConfiguration.classList.add("hidden");
    $("#apiKeyField").classList.remove("hidden");
    $("#connectButton").classList.remove("hidden");
    $("#selectionModeMessage").classList.add("hidden");
    $("#cancelSelectionButton").classList.add("hidden");
    apiKeyInput.required = true;
    $("#tenantForm").elements.display_name.value = "";
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
      const actions = document.createElement("div");
      actions.className = "tenant-actions";
      const deploy = document.createElement("button");
      deploy.type = "button";
      deploy.className = "primary compact-button";
      deploy.textContent = "查看下一步部署";
      deploy.addEventListener("click", () => showDeploymentGuide(tenant));
      actions.append(deploy);
      if (!tenant.enabled && tenant.jiandaoyun.mapping_configured) {
        const manage = document.createElement("button");
        manage.type = "button";
        manage.className = "ghost compact-button";
        manage.textContent = "查看并确认字段";
        manage.addEventListener("click", async () => {
          oneTimeCredentials = null;
          await confirmFields(tenant.tenant_id, {}, manage, saveMessage);
        });
        actions.append(manage);
      }
      if (tenant.jiandaoyun.api_key_configured) {
        const changeForm = document.createElement("button");
        changeForm.type = "button";
        changeForm.className = "ghost compact-button";
        changeForm.textContent = "更换表单";
        changeForm.addEventListener("click", async () => {
          oneTimeCredentials = null;
          await startExistingFormSelection(tenant, changeForm);
        });
        actions.append(changeForm);
      }
      row.append(actions);
      return row;
    }));
  }

  function populateForms() {
    const applicationId = $("#applicationSelect").value;
    const application = authorizedApplications.find((item) => item.app_id === applicationId);
    const formSelect = $("#formSelect");
    formSelect.replaceChildren();
    let firstAvailable = null;
    (application?.forms || []).forEach((form) => {
      const option = document.createElement("option");
      option.value = form.entry_id;
      option.disabled = Boolean(form.already_connected);
      option.textContent = form.already_connected
        ? `${form.name}（已接入：${form.connected_display_name || "其他客户"}）`
        : form.name;
      if (!option.disabled && firstAvailable === null) firstAvailable = form.entry_id;
      formSelect.append(option);
    });
    if (firstAvailable !== null) {
      formSelect.value = firstAvailable;
    } else {
      const emptyOption = document.createElement("option");
      emptyOption.value = "";
      emptyOption.textContent = "该应用下的表单均已接入（同一个表单只能接入一次）";
      emptyOption.selected = true;
      formSelect.prepend(emptyOption);
    }
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

  function useAuthorizedApplications(result) {
    authorizedApplications = (result.applications || []).filter((application) => application.forms?.length);
    if (!authorizedApplications.length) {
      throw new Error("当前授权范围内没有可用表单，请先在简道云中授权应用。");
    }
    authorizationReady = true;
    populateApplications();
  }

  async function startExistingFormSelection(tenant, button) {
    clearMessage(saveMessage);
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "正在读取授权表单…";
    try {
      const result = await request(
        `/api/v1/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/jiandaoyun/authorization`,
      );
      resetAuthorization();
      useAuthorizedApplications(result);
      editingTenant = tenant;
      const currentApplicationId = tenant.jiandaoyun?.application_id;
      const currentEntryId = tenant.jiandaoyun?.entry_id;
      if (currentApplicationId) {
        $("#applicationSelect").value = currentApplicationId;
        populateForms();
      }
      if (currentEntryId) $("#formSelect").value = currentEntryId;
      apiKeyInput.value = "";
      apiKeyInput.required = false;
      $("#apiKeyField").classList.add("hidden");
      $("#connectButton").classList.add("hidden");
      $("#cancelSelectionButton").classList.remove("hidden");
      const modeMessage = $("#selectionModeMessage");
      modeMessage.textContent = `正在为“${tenant.display_name}”重新选择表单；系统客户编号和现有密钥保持不变。`;
      modeMessage.classList.remove("hidden");
      $("#tenantForm").elements.display_name.value = tenant.display_name;
      formConfiguration.classList.remove("hidden");
      showMessage(connectionMessage, "已使用该客户现有授权重新读取应用和表单。", true);
      formConfiguration.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showMessage(saveMessage, error.message);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
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
    oneTimeCredentials = null;
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
      useAuthorizedApplications(result);
      connectedApiKey = apiKey;
      formConfiguration.classList.remove("hidden");
      const formCount = authorizedApplications.reduce((total, application) => total + application.forms.length, 0);
      const connectedCount = authorizedApplications.reduce(
        (total, application) => total + application.forms.filter((form) => form.already_connected).length,
        0,
      );
      const connectedHint = connectedCount ? `；其中 ${connectedCount} 个表单已接入，不能重复选择` : "";
      showMessage(connectionMessage, `连接成功，已读取 ${authorizedApplications.length} 个应用、${formCount} 个表单${connectedHint}。`, true);
    } catch (error) {
      resetAuthorization();
      showMessage(connectionMessage, error.message);
    } finally {
      button.disabled = false;
      button.textContent = "连接简道云";
    }
  });

  $("#cancelSelectionButton").addEventListener("click", () => {
    resetAuthorization();
    apiKeyInput.value = "";
    showMessage(saveMessage, "已取消重新选择表单，原配置保持不变。", true);
  });

  $("#tenantForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const onboardingForm = event.currentTarget;
    clearMessage(saveMessage);
    if (!authorizationReady || !authorizedApplications.length) {
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
    const existingTenant = editingTenant;
    const payload = {
      tenant_id: existingTenant?.tenant_id || null,
      display_name: onboardingForm.elements.display_name.value.trim(),
      application_id: application.app_id,
      entry_id: selectedForm.entry_id,
      entry_name: selectedForm.name,
      jiandaoyun_api_key: existingTenant ? null : connectedApiKey,
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
      if (result.one_time_credentials.access_key || result.one_time_credentials.webhook_secret) {
        oneTimeCredentials = result.one_time_credentials;
      }
      renderResult(result);
      const stateText = existingTenant
        ? (result.activated ? "表单配置已更新并立即生效" : "表单配置已更新，请确认未匹配字段")
        : (result.activated ? "配置已写入并立即生效" : "配置已保存，请根据提示确认未匹配字段");
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

    const reselect = document.createElement("button");
    reselect.type = "button";
    reselect.className = "ghost result-action";
    reselect.textContent = "返回重新选择表单";
    reselect.addEventListener("click", async () => {
      await startExistingFormSelection(result.tenant, reselect);
    });
    target.append(reselect);

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

    const credentials = oneTimeCredentials || result.one_time_credentials;
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

    if (result.mapping_report.unresolved?.length) {
      renderUnresolvedFields(target, result);
    }

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
    renderDeploymentGuide(
      target,
      result.tenant,
      result.activated,
      credentials,
      result.mapping_report.unresolved_count || 0,
    );
  }

  function showDeploymentGuide(tenant) {
    $("#emptyResult").classList.add("hidden");
    const target = $("#resultContent");
    target.classList.remove("hidden");
    target.replaceChildren();
    const badge = document.createElement("div");
    badge.className = `result-badge ${tenant.enabled ? "active" : "pending"}`;
    badge.textContent = tenant.enabled ? "字段配置已完成" : "字段配置尚未完成";
    const identifier = document.createElement("p");
    identifier.className = "generated-id";
    identifier.textContent = `${tenant.display_name} · 系统客户编号：${tenant.tenant_id}`;
    target.append(badge, identifier);
    renderDeploymentGuide(target, tenant, tenant.enabled, null, tenant.enabled ? 0 : null);
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderDeploymentGuide(target, tenant, activated, credentials, unresolvedCount) {
    const panel = document.createElement("section");
    panel.className = "deployment-panel";
    const title = document.createElement("h3");
    title.textContent = "下一步：完成该客户的简道云部署";
    const intro = document.createElement("p");
    intro.className = activated ? "deployment-ready" : "deployment-waiting";
    intro.textContent = activated
      ? "表单字段已经匹配，可以继续配置AI检测按钮和提交后深度评价。"
      : "请先处理全部待确认字段；字段确认完成后，再执行下面的简道云配置。";
    panel.append(title, intro);

    const steps = document.createElement("ol");
    steps.className = "deployment-steps";
    const fieldText = activated
      ? "字段映射已完成，客户已启用。"
      : (unresolvedCount === null
          ? "点击客户列表中的“查看并确认字段”，处理完所有待确认项。"
          : `先处理 ${unresolvedCount} 个待确认字段，全部确认后客户会自动启用。`);
    addDeploymentStep(steps, "确认字段配置", fieldText, activated);
    addDeploymentStep(
      steps,
      "保存接入密钥",
      credentials?.access_key || credentials?.webhook_secret
        ? "立即复制上方的TAORAN访问Key和推送签名密钥；两项密钥只显示一次。"
        : "准备首次接入时已经保存的TAORAN访问Key和推送签名密钥；系统不会再次显示明文。",
      false,
    );
    addDeploymentStep(
      steps,
      "配置“AI检测”插件",
      "在简道云插件/前端事件中填写下面的检测接口、系统客户编号和TAORAN访问Key，并绑定表单输入字段及三个AI反馈输出字段。",
      false,
    );
    addDeploymentStep(
      steps,
      "配置提交后深度评价",
      "在该表单新建数据推送“TAORAN提交后深度评价”，监听数据新增和数据修改，填写下面的推送地址及推送签名密钥。",
      false,
    );
    addDeploymentStep(
      steps,
      "完成真实测试后上线",
      "先用一条专用测试记录点击AI检测，再提交记录；确认AI评分和各反馈字段成功回写且没有重复触发，最后再切换到正式表单。",
      false,
    );
    panel.append(steps);

    const values = document.createElement("div");
    values.className = "deployment-values";
    addDeploymentValue(values, "AI检测接口", `${window.location.origin}/api/v1/connectors/jiandaoyun/visit/button-check`);
    addDeploymentValue(values, "系统客户编号", tenant.tenant_id);
    addDeploymentValue(values, "提交后推送地址", `${window.location.origin}/api/v1/connectors/jiandaoyun/visit/webhook?tenant_id=${encodeURIComponent(tenant.tenant_id)}`);
    panel.append(values);

    const reminder = document.createElement("p");
    reminder.className = "deployment-reminder";
    reminder.textContent = "注意：API Key使用TAORAN访问Key；数据推送签名使用推送签名密钥，两者不能混用。";
    panel.append(reminder);
    target.append(panel);
  }

  function addDeploymentStep(parent, titleText, detailText, completed) {
    const item = document.createElement("li");
    if (completed) item.className = "completed";
    const title = document.createElement("strong");
    title.textContent = titleText;
    const detail = document.createElement("span");
    detail.textContent = detailText;
    item.append(title, detail);
    parent.append(item);
  }

  function addDeploymentValue(parent, labelText, value) {
    const row = document.createElement("div");
    row.className = "deployment-value";
    const label = document.createElement("strong");
    label.textContent = labelText;
    const code = document.createElement("code");
    code.textContent = value;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "ghost compact-button";
    copy.textContent = "复制";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(value);
      copy.textContent = "已复制";
    });
    row.append(label, code, copy);
    parent.append(row);
  }

  function renderUnresolvedFields(target, result) {
    const panel = document.createElement("section");
    panel.className = "unresolved-panel";
    const title = document.createElement("h3");
    title.textContent = "待确认字段明细";
    const guidance = document.createElement("p");
    guidance.className = "hint";
    guidance.textContent = "可为待确认项选择同一表单范围内的实际字段；也可先在简道云新增或改名，再重新检查。";
    panel.append(title, guidance);

    const matchedIds = new Set(
      (result.mapping_report.matched || []).map((item) => item.widget_id).filter(Boolean),
    );
    const availableFields = result.mapping_report.available_fields || [];
    result.mapping_report.unresolved.forEach((item) => {
      const row = document.createElement("div");
      row.className = "unresolved-row";
      const summary = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = item.field_name;
      const location = document.createElement("span");
      location.className = "field-location";
      location.textContent = `${item.location || "表单"} · ${item.path}`;
      summary.append(name, location);

      const select = document.createElement("select");
      select.dataset.mappingPath = item.path;
      select.setAttribute("aria-label", `${item.field_name}对应的简道云字段`);
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "请选择对应的简道云实际字段";
      select.append(placeholder);
      const candidates = availableFields.filter(
        (field) => field.scope === item.candidate_scope
          && !matchedIds.has(field.widget_id)
          && compatibleFieldType(item.expected_widget_type, field.widget_type),
      );
      candidates.forEach((field) => {
        const option = document.createElement("option");
        option.value = field.widget_id;
        const parent = field.parent_name ? `${field.parent_name} / ` : "";
        option.textContent = `${parent}${field.field_name}${field.widget_type ? `（${field.widget_type}）` : ""}`;
        select.append(option);
      });
      if (!candidates.length) {
        placeholder.textContent = "同一范围内没有可选字段，请先修改简道云表单";
      }
      row.append(summary, select);
      panel.append(row);
    });

    const message = document.createElement("div");
    message.className = "message hidden";
    message.setAttribute("role", "alert");
    const actions = document.createElement("div");
    actions.className = "confirmation-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "保存手动映射并重新检查";
    save.addEventListener("click", async () => {
      const assignments = {};
      panel.querySelectorAll("select[data-mapping-path]").forEach((select) => {
        if (select.value) assignments[select.dataset.mappingPath] = select.value;
      });
      if (!Object.keys(assignments).length) {
        showMessage(message, "请至少选择一个待确认字段；如果已在简道云修改字段，请使用“重新读取表单字段”。");
        return;
      }
      await confirmFields(result.tenant.tenant_id, assignments, save, message);
    });
    const recheck = document.createElement("button");
    recheck.type = "button";
    recheck.className = "ghost";
    recheck.textContent = "重新读取表单字段";
    recheck.addEventListener("click", async () => {
      await confirmFields(result.tenant.tenant_id, {}, recheck, message);
    });
    actions.append(save, recheck);
    panel.append(actions, message);
    target.append(panel);
  }

  function compatibleFieldType(expected, actual) {
    if (!expected || !actual || expected === actual) return true;
    return [expected, actual].every((type) => ["text", "textarea", "number"].includes(type));
  }

  async function confirmFields(tenantId, assignments, button, message) {
    clearMessage(message);
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = "正在重新检查字段…";
    try {
      const result = await request(
        `/api/v1/admin/tenants/${encodeURIComponent(tenantId)}/field-confirmation`,
        { method: "POST", body: JSON.stringify({ assignments }) },
      );
      renderResult(result);
      const text = result.activated
        ? "全部必需字段已确认，客户已自动启用。"
        : `已重新检查，仍有 ${result.mapping_report.unresolved_count} 个字段待确认。`;
      showMessage(saveMessage, text, true);
      await loadTenants();
    } catch (error) {
      showMessage(message, error.message);
      button.disabled = false;
      button.textContent = originalText;
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
