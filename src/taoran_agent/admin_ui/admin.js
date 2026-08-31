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
      const runtime = document.createElement("button");
      runtime.type = "button";
      runtime.className = "ghost compact-button";
      runtime.textContent = "运行状态";
      runtime.addEventListener("click", async () => {
        await showView("runtime", tenant.tenant_id);
      });
      actions.append(runtime);
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

  async function showView(view, focusTenantId = null) {
    const runtimeView = $("#runtimeView");
    const onboardingView = $("#onboardingView");
    const showingRuntime = view === "runtime";
    runtimeView.classList.toggle("hidden", !showingRuntime);
    onboardingView.classList.toggle("hidden", showingRuntime);
    $("#showRuntimeButton").classList.toggle("active", showingRuntime);
    $("#showOnboardingButton").classList.toggle("active", !showingRuntime);
    if (showingRuntime) {
      await loadRuntimeStatus();
      if (focusTenantId) {
        document.getElementById(`runtime-${focusTenantId}`)?.scrollIntoView({
          behavior: "smooth",
          block: "start",
        });
      }
    }
  }

  async function loadRuntimeStatus() {
    const message = $("#runtimeMessage");
    clearMessage(message);
    const button = $("#refreshRuntimeButton");
    button.disabled = true;
    button.textContent = "正在读取运行记录…";
    try {
      const data = await request("/api/v1/admin/runtime-status");
      renderSystemRuntime(data.system, data.generated_at);
      renderTenantRuntime(data.tenants || []);
    } catch (error) {
      showMessage(message, error.message);
    } finally {
      button.disabled = false;
      button.textContent = "刷新运行状态";
    }
  }

  function renderSystemRuntime(system, generatedAt) {
    const cards = $("#systemRuntimeCards");
    const items = [
      ["TAORAN服务", system.service_status === "ok" ? "运行正常" : "异常", system.service_status === "ok"],
      ["当前版本", `v${system.version}`, true],
      ["大模型", system.llm_enabled ? `${system.llm_model} · 已启用` : "未启用", system.llm_enabled],
      ["DSM知识库", system.knowledge_api_configured ? "已连接" : "未配置", system.knowledge_api_configured],
    ];
    cards.replaceChildren(...items.map(([label, value, ok]) => {
      const card = document.createElement("div");
      card.className = "runtime-system-card";
      const caption = document.createElement("span");
      caption.textContent = label;
      const result = document.createElement("strong");
      result.className = ok ? "runtime-ok" : "runtime-warn";
      result.textContent = value;
      card.append(caption, result);
      return card;
    }));
    const updated = document.createElement("p");
    updated.className = "runtime-updated";
    updated.textContent = `状态更新时间：${formatRuntimeTime(generatedAt)}`;
    cards.append(updated);
  }

  function renderTenantRuntime(tenants) {
    const list = $("#tenantRuntimeList");
    if (!tenants.length) {
      const empty = document.createElement("div");
      empty.className = "card empty";
      empty.textContent = "尚未接入客户，完成客户接入后这里会显示运行状态。";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...tenants.map(buildTenantRuntimeCard));
  }

  function buildTenantRuntimeCard(tenant) {
    const state = runtimeState(tenant.deployment_state);
    const activity = tenant.activity;
    const precheck = activity.precheck;
    const evaluation = activity.evaluation;
    const latestCheck = precheck.latest;
    const latestEvaluation = evaluation.latest;
    const card = document.createElement("article");
    card.id = `runtime-${tenant.tenant_id}`;
    card.className = "card runtime-tenant-card";

    const heading = document.createElement("div");
    heading.className = "runtime-tenant-heading";
    const identity = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = tenant.display_name;
    const meta = document.createElement("p");
    meta.className = "hint";
    meta.textContent = `${tenant.jiandaoyun.entry_name || "表单待确认"} · ${tenant.tenant_id}`;
    identity.append(title, meta);
    const badge = document.createElement("span");
    badge.className = `runtime-state ${state.kind}`;
    badge.textContent = state.label;
    heading.append(identity, badge);
    card.append(heading);

    const progress = document.createElement("div");
    progress.className = "runtime-progress";
    addRuntimeStage(progress, "1", "客户配置", tenant.enabled ? "字段已确认，客户已启用" : "仍有字段待确认", tenant.enabled);
    addRuntimeStage(
      progress,
      "2",
      "AI检测按钮",
      precheck.total_count
        ? `已收到 ${precheck.total_count} 次检测；最近 ${formatRuntimeTime(latestCheck?.created_at)}`
        : "尚未收到真实AI检测",
      precheck.total_count > 0,
    );
    addRuntimeStage(
      progress,
      "3",
      "提交后评价",
      evaluation.total_count
        ? `共 ${evaluation.total_count} 次；最近状态：${evaluationStatusText(latestEvaluation?.status)}`
        : "尚未收到表单提交事件",
      latestEvaluation?.status === "completed",
    );
    addRuntimeStage(
      progress,
      "4",
      "简道云回写",
      latestEvaluation
        ? `最近回写：${writebackStatusText(latestEvaluation.writeback_status)}`
        : "尚无回写记录",
      latestEvaluation?.writeback_status === "succeeded",
    );
    card.append(progress);

    const next = document.createElement("p");
    next.className = `runtime-next ${state.kind}`;
    next.textContent = `下一步：${state.next}`;
    card.append(next);

    const facts = document.createElement("div");
    facts.className = "runtime-facts";
    addRuntimeFact(facts, "最近AI检测", latestCheck ? `${latestCheck.result_status || "已完成"} · ${latestCheck.semantic_model || "本地规则"}` : "无记录");
    addRuntimeFact(facts, "最近评分", latestEvaluation?.total_score == null ? "无记录" : `${latestEvaluation.total_score}/100（Q33 ${latestEvaluation.q33_score}，Q34 ${latestEvaluation.q34_score}）`);
    addRuntimeFact(facts, "评价任务统计", `完成 ${evaluation.completed_count} · 失败 ${evaluation.failed_count} · 处理中 ${evaluation.pending_count}`);
    addRuntimeFact(facts, "成功回写次数", String(evaluation.writeback_succeeded_count));
    card.append(facts);

    const problem = latestEvaluation?.writeback_error || latestEvaluation?.job_error || latestEvaluation?.failure_reason || latestCheck?.failure_reason;
    if (problem) {
      const warning = document.createElement("p");
      warning.className = "runtime-problem";
      warning.textContent = `最近异常：${problem}`;
      card.append(warning);
    }
    return card;
  }

  function addRuntimeStage(parent, number, titleText, detailText, completed) {
    const item = document.createElement("div");
    item.className = `runtime-stage ${completed ? "completed" : "pending"}`;
    const numberBadge = document.createElement("span");
    numberBadge.textContent = completed ? "✓" : number;
    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = titleText;
    const detail = document.createElement("small");
    detail.textContent = detailText;
    text.append(title, detail);
    item.append(numberBadge, text);
    parent.append(item);
  }

  function addRuntimeFact(parent, labelText, valueText) {
    const item = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = labelText;
    const value = document.createElement("strong");
    value.textContent = valueText;
    item.append(label, value);
    parent.append(item);
  }

  function runtimeState(state) {
    return ({
      configuration_pending: { label: "配置未完成", kind: "warning", next: "返回客户接入配置，处理全部待确认字段。" },
      awaiting_plugin_test: { label: "等待AI检测测试", kind: "waiting", next: "在简道云草稿页点击一次AI检测按钮。" },
      awaiting_submission_test: { label: "等待提交测试", kind: "waiting", next: "提交一条专用测试记录，验证深度评价和回写。" },
      evaluation_running: { label: "评价处理中", kind: "waiting", next: "稍后刷新本页查看评价和回写结果。" },
      evaluation_failed: { label: "评价失败", kind: "danger", next: "检查最近异常，修复后重新提交测试记录。" },
      writeback_attention: { label: "回写待处理", kind: "danger", next: "检查推送签名、输出字段和简道云API写入权限。" },
      operational: { label: "部署完成 · 运行正常", kind: "success", next: "保持监控；正式迁移前再完成一次业务验收。" },
    })[state] || { label: "状态未知", kind: "warning", next: "刷新页面或联系管理员检查服务。" };
  }

  function evaluationStatusText(status) {
    return ({ queued: "等待中", running: "处理中", completed: "已完成", failed: "失败" })[status] || status || "未知";
  }

  function writebackStatusText(status) {
    return ({ succeeded: "成功", failed: "失败", skipped: "未执行" })[status] || "尚未执行";
  }

  function formatRuntimeTime(value) {
    if (!value) return "无记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
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
    $("#runtimeView").classList.add("hidden");
    $("#onboardingView").classList.remove("hidden");
    $("#showRuntimeButton").classList.remove("active");
    $("#showOnboardingButton").classList.add("active");
  });

  $("#refreshButton").addEventListener("click", async () => {
    try { await loadTenants(); } catch (error) { showMessage(saveMessage, error.message); }
  });

  $("#showOnboardingButton").addEventListener("click", async () => {
    await showView("onboarding");
  });

  $("#showRuntimeButton").addEventListener("click", async () => {
    await showView("runtime");
  });

  $("#refreshRuntimeButton").addEventListener("click", loadRuntimeStatus);

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

    const fieldText = activated
      ? "字段映射已完成，客户已启用。"
      : (unresolvedCount === null
          ? "点击客户列表中的“查看并确认字段”，处理完所有待确认项。"
          : `先处理 ${unresolvedCount} 个待确认字段，全部确认后客户会自动启用。`);
    const values = document.createElement("div");
    values.className = "deployment-values";
    addDeploymentValue(values, "AI检测接口", `${window.location.origin}/api/v1/connectors/jiandaoyun/visit/button-check`);
    addDeploymentValue(values, "系统客户编号", tenant.tenant_id);
    addDeploymentValue(values, "提交后推送地址", `${window.location.origin}/api/v1/connectors/jiandaoyun/visit/webhook?tenant_id=${encodeURIComponent(tenant.tenant_id)}`);
    panel.append(values);

    const copyGuide = document.createElement("button");
    copyGuide.type = "button";
    copyGuide.className = "ghost copy-guide";
    copyGuide.textContent = "复制本客户部署清单";
    copyGuide.addEventListener("click", async () => {
      const checklist = deploymentChecklistText(tenant);
      await navigator.clipboard.writeText(checklist);
      copyGuide.textContent = "部署清单已复制";
    });
    panel.append(copyGuide);

    const tutorial = document.createElement("div");
    tutorial.className = "deployment-tutorial";
    addTutorialStep(tutorial, 1, "确认字段映射并启用客户", [
      fieldText,
      "如果状态是“未启用”，点击客户列表中的“查看并确认字段”。",
      "逐项选择对应的简道云实际字段；简道云缺少字段时，先到表单设计器新增或改名，再点击“重新读取表单字段”。",
      "确认“待确认字段”为0，并看到“客户已启用”后再进入下一步。",
    ], activated ? "验收标准：客户状态为“已启用”，待确认字段为0。" : "当前未完成：插件和数据推送可以先查看，但不要进入正式测试。", true);

    addTutorialStep(tutorial, 2, "保存并区分两类密钥", [
      credentials?.access_key || credentials?.webhook_secret
        ? "立即复制配置结果上方的“TAORAN访问Key”和“推送签名密钥”，它们只显示一次。"
        : "找到首次接入时保存的“TAORAN访问Key”和“推送签名密钥”；关闭首次结果后系统不会再次显示明文。",
      "把密钥保存到公司密码管理器，并在名称中注明客户名称和系统客户编号。",
      "TAORAN访问Key只用于AI检测插件；推送签名密钥只用于提交后数据推送，不能互换。",
    ], "验收标准：实施人员可以分别找到两项密钥，但页面、群聊和文档中没有明文泄露。", false);

    addTutorialStep(tutorial, 3, "安装并配置TAORAN AI检测插件", [
      "进入简道云管理后台，打开“插件管理/插件中心”，安装或打开公司统一提供的“TAORAN拜访草稿检查”插件。",
      "如果需要新建自建插件：建立后端函数，运行环境选择Node.js 20，粘贴公司统一版本代码并启用；不要自行修改请求地址和认证逻辑。",
      "在插件通用参数中创建 endpoint_url、tenant_id、api_key 三个文本参数。",
      "endpoint_url填写上方“AI检测接口”；tenant_id填写“系统客户编号”；api_key填写“TAORAN访问Key”。",
      "保存插件并运行一次函数调试，确认不是“服务地址或授权配置不正确”。",
    ], "验收标准：插件已启用，函数调试可以连接TAORAN服务并返回本次检查结果。", false);

    const bindingStep = addTutorialStep(tutorial, 4, "在表单中添加AI检测按钮并绑定字段", [
      "进入已选择的拜访记录表单设计器，在“前端事件”中保留一个“AI检测”按钮，不要同时保留旧自定义请求和新插件两个动作。",
      "按钮动作选择“TAORAN拜访草稿检查”插件，将下表参数绑定到当前表单字段。多行文本必须直接绑定字段值，不能改成固定文本。",
      "联系人信息、关联商机阶段信息必须使用“按子表单赋值”，逐行绑定子字段，不能把整个子表当普通文本。",
      "把三个插件返回值分别写入三个AI反馈字段；提交前按钮不要写入“AI评分”。",
      "保存表单后关闭设计器，再重新打开一次，确认按钮动作和所有字段绑定仍然存在。",
    ], "验收标准：草稿页点击一次AI检测，规则、知识库、大模型三个反馈字段都有本次内容。", false);
    addBindingTables(bindingStep);

    addTutorialStep(tutorial, 5, "配置提交后深度评价数据推送", [
      "进入该拜访记录表单的“数据推送”设置，新建推送，名称填写“TAORAN提交后深度评价”。",
      "触发事件同时勾选“数据新增”和“数据修改”，这样首次提交和后续修订都能重新评价。",
      "请求方式选择POST，推送地址复制上方“提交后推送地址”。",
      "签名/密钥位置填写“推送签名密钥”，不要填写TAORAN访问Key。",
      "保存并启用数据推送；如果简道云提供连接测试，确认运行记录为成功。",
    ], "验收标准：数据推送处于启用状态，目标地址包含本客户系统编号，新增和修改均会触发。", false);

    const testStep = addTutorialStep(tutorial, 6, "使用专用测试记录完成上线验收", [
      "先在测试副本新增一条专用记录，完整填写拜访目的、关键结果、过程详细描述、评价和下一次行动。",
      "提交前点击“AI检测”，确认按钮有反馈；修改过程描述后再次点击，确认结果会更新，不会沿用上一次内容。",
      "提交记录，等待深度评价完成，再刷新记录查看AI评分及三个AI反馈字段是否回写。",
      "修改同一条记录后再次提交，确认可以形成新评价，同时TAORAN自身回写不会造成无限循环。",
      "记录客户、表单、测试时间和结果；全部通过后，才把相同配置迁移到正式拜访记录表。",
    ], "验收标准：按钮检测、提交后评分、四个输出字段回写和重复提交均通过，且没有影响其他表单。", false);
    addTroubleshooting(testStep);
    panel.append(tutorial);

    const reminder = document.createElement("p");
    reminder.className = "deployment-reminder";
    reminder.textContent = "注意：API Key使用TAORAN访问Key；数据推送签名使用推送签名密钥，两者不能混用。";
    panel.append(reminder);
    target.append(panel);
  }

  function addTutorialStep(parent, number, titleText, instructions, successText, open) {
    const details = document.createElement("details");
    details.className = "tutorial-step";
    details.open = open;
    const summary = document.createElement("summary");
    const numberBadge = document.createElement("span");
    numberBadge.className = "tutorial-number";
    numberBadge.textContent = String(number);
    const title = document.createElement("strong");
    title.textContent = titleText;
    summary.append(numberBadge, title);
    const body = document.createElement("div");
    body.className = "tutorial-body";
    const list = document.createElement("ol");
    instructions.forEach((instruction) => {
      const item = document.createElement("li");
      item.textContent = instruction;
      list.append(item);
    });
    const success = document.createElement("p");
    success.className = "tutorial-success";
    success.textContent = successText;
    body.append(list, success);
    details.append(summary, body);
    parent.append(details);
    return body;
  }

  function addBindingTables(parent) {
    addMappingTable(parent, "普通字段参数（逐项绑定当前表单字段）", [
      ["visit_date", "拜访日期"], ["employee_id", "销售代表（通讯录）"],
      ["customer_id", "客户编号"], ["customer_type_ii", "客户分类II"],
      ["visit_method", "拜访方式"], ["is_appointment", "是否预约"],
      ["purpose_code", "拜访目的"], ["other_purpose", "具体其他目的"],
      ["expected_key_result", "想取得的关键结果"], ["process_description", "过程详细描述"],
      ["self_assessment", "评价"], ["next_action_purpose", "下一次行动目的"],
      ["next_action_other_purpose", "下一次具体其他目的"],
      ["next_action_expected_result", "下次拜访期望的关键结果"],
      ["next_contact_at", "下一次联系客户时间安排"],
      ["actual_start_at", "实际拜访开始时间"], ["actual_end_at", "实际拜访结束时间"],
      ["duration_minutes", "拜访时长"], ["evidence_ids", "上传相关文件"],
    ]);
    addMappingTable(parent, "子表参数（选择“按子表单赋值”）", [
      ["participants.contact_id", "联系人信息 / 关联数据-主键"],
      ["opportunities.opportunity_id", "关联商机阶段信息 / 商机编号"],
      ["opportunities.historical_stage", "关联商机阶段信息 / 历史商机阶段"],
      ["opportunities.current_stage", "关联商机阶段信息 / 最新商机阶段"],
    ]);
    addMappingTable(parent, "插件返回值（分别写入两个反馈字段）", [
      ["rule_feedback_text", "AI反馈意见（规则反馈）"],
      ["knowledge_feedback_text", "AI反馈意见（知识库反馈）"],
    ]);
  }

  function addMappingTable(parent, titleText, rows) {
    const details = document.createElement("details");
    details.className = "mapping-details";
    const summary = document.createElement("summary");
    summary.textContent = `${titleText}（${rows.length}项）`;
    const table = document.createElement("div");
    table.className = "tutorial-mapping-table";
    rows.forEach(([parameter, field]) => {
      const parameterCell = document.createElement("code");
      parameterCell.textContent = parameter;
      const fieldCell = document.createElement("span");
      fieldCell.textContent = field;
      table.append(parameterCell, fieldCell);
    });
    details.append(summary, table);
    parent.append(details);
  }

  function addTroubleshooting(parent) {
    addMappingTable(parent, "常见问题检查顺序", [
      ["AI检测无反应", "检查插件是否启用、云币余额、三个通用参数、按钮动作和12秒超时"],
      ["提示接口未获取", "检查过程详细描述、通讯录和两个子表是否按正确类型绑定"],
      ["提交后没有评分", "检查数据推送已启用、监听新增/修改、地址中的客户编号及推送签名密钥"],
      ["反馈有但评分为空", "查看深度评价是否仍在运行，稍后刷新；持续失败由管理员检查任务日志"],
      ["重复触发", "确认TAORAN回写未再次启动业务触发，并且表单中只保留一个数据推送"],
    ]);
  }

  function deploymentChecklistText(tenant) {
    const origin = window.location.origin;
    return [
      `TAORAN客户部署清单：${tenant.display_name}`,
      `系统客户编号：${tenant.tenant_id}`,
      `AI检测接口：${origin}/api/v1/connectors/jiandaoyun/visit/button-check`,
      `提交后推送地址：${origin}/api/v1/connectors/jiandaoyun/visit/webhook?tenant_id=${tenant.tenant_id}`,
      "1. 确认待确认字段为0，客户状态为已启用。",
      "2. 分别保存TAORAN访问Key和推送签名密钥。",
      "3. 配置并启用TAORAN拜访草稿检查插件。",
      "4. 为AI检测按钮绑定普通字段、联系人子表、商机子表和三个反馈输出。",
      "5. 配置“TAORAN提交后深度评价”数据推送，监听数据新增和修改。",
      "6. 使用专用记录完成按钮、提交、回写和再次提交测试。",
      "安全提醒：本清单不包含密钥；密钥请从密码管理器获取。",
    ].join("\n");
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
