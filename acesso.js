(() => {
  const $ = (selector) => document.querySelector(selector);
  const views = ["loginView", "adminView", "employeeView"];
  const statusNames = { draft: "Preparação", open: "Aberta", closed: "Encerrada" };
  let adminSetupRequired = false;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      headers: options.payload ? { "Content-Type": "application/json" } : {},
      body: options.payload ? JSON.stringify(options.payload) : undefined,
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Não foi possível concluir a operação.");
    return body;
  }

  function showView(id) {
    views.forEach((viewId) => $(`#${viewId}`).classList.toggle("hidden", viewId !== id));
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function showMessage(id, text) {
    $(`#${id}`).textContent = text;
  }

  function addEntry(list, title, detail, label, remove) {
    const item = document.createElement("li");
    item.className = "entry";
    const copy = document.createElement("div");
    copy.className = "entry-copy";
    const heading = document.createElement("strong");
    heading.textContent = title;
    const description = document.createElement("span");
    description.textContent = detail;
    copy.append(heading, description);
    item.append(copy);
    if (remove) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "remove-button";
      button.textContent = "Remover";
      button.setAttribute("aria-label", `${label} ${title}`);
      button.addEventListener("click", remove);
      item.append(button);
    }
    list.append(item);
  }

  async function renderAdmin() {
    const overview = await api("/api/admin/overview");
    $("#employeeTotal").textContent = overview.employees.length;
    $("#candidateTotal").textContent = overview.candidates.length;
    $("#electionStatus").textContent = statusNames[overview.status];
    $("#employeeEmpty").classList.toggle("hidden", overview.employees.length > 0);
    $("#candidateEmpty").classList.toggle("hidden", overview.candidates.length > 0);

    const employeeList = $("#employeeList");
    const candidateList = $("#candidateList");
    employeeList.replaceChildren();
    candidateList.replaceChildren();

    overview.employees.forEach((employee) => {
      const state = employee.hasVoted ? "Voto registrado" : "Ainda não votou";
      addEntry(
        employeeList,
        employee.name,
        `${employee.registration} · ${employee.sector} · ${state}`,
        "Remover funcionário",
        overview.status === "draft"
          ? async () => {
              try {
                await api(`/api/admin/employees/${encodeURIComponent(employee.id)}`, { method: "DELETE" });
                await renderAdmin();
              } catch (error) {
                showMessage("employeeFormMessage", error.message);
              }
            }
          : null,
      );
    });

    overview.candidates.forEach((candidate) => {
      addEntry(
        candidateList,
        candidate.name,
        `${candidate.role} · ${candidate.pitch}`,
        "Remover candidato",
        overview.status === "draft"
          ? async () => {
              try {
                await api(`/api/admin/candidates/${encodeURIComponent(candidate.id)}`, { method: "DELETE" });
                await renderAdmin();
              } catch (error) {
                showMessage("candidateFormMessage", error.message);
              }
            }
          : null,
      );
    });

    const isDraft = overview.status === "draft";
    $("#employeeForm").querySelectorAll("input, button").forEach((item) => { item.disabled = !isDraft; });
    $("#candidateForm").querySelectorAll("input, textarea, button").forEach((item) => { item.disabled = !isDraft; });
    $("#openElectionButton").classList.toggle("hidden", !isDraft);
    $("#closeElectionButton").classList.toggle("hidden", overview.status !== "open");
    $("#newElectionButton").classList.toggle("hidden", overview.status !== "closed");
    $("#electionControlMessage").textContent = isDraft
      ? `Eleição nº ${overview.electionId}: confira os eleitores e candidaturas. Ao abrir, as alterações de cadastro serão bloqueadas.`
      : overview.status === "open"
        ? "A votação está aberta. Novos cadastros e alterações estão bloqueados."
        : "A votação foi encerrada definitivamente. A apuração está disponível; você pode iniciar uma nova eleição sem apagar este histórico.";

    const resultsPanel = $("#resultsPanel");
    resultsPanel.classList.toggle("hidden", overview.status !== "closed");
    if (overview.results) {
      const results = $("#resultList");
      results.replaceChildren();
      overview.results.candidates.forEach((candidate) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        const count = document.createElement("strong");
        name.textContent = candidate.name;
        count.textContent = `${candidate.votes} voto(s)`;
        item.append(name, count);
        results.append(item);
      });
      const blank = document.createElement("li");
      blank.append(document.createTextNode("Votos em branco"));
      const blankCount = document.createElement("strong");
      blankCount.textContent = `${overview.results.blankVotes} voto(s)`;
      blank.append(blankCount);
      results.append(blank);
      $("#resultSummary").textContent =
        `Votos registrados: ${overview.results.castVotes} · Participação: ${overview.results.turnout} de ${overview.results.eligibleVoters} eleitores.`;
    }

    const historyPanel = $("#historyPanel");
    const historyList = $("#historyList");
    historyPanel.classList.toggle("hidden", overview.history.length === 0);
    historyList.replaceChildren();
    overview.history.forEach((election) => {
      const item = document.createElement("article");
      item.className = "history-item";
      const heading = document.createElement("h3");
      heading.textContent = `Eleição nº ${election.id}`;
      const date = document.createElement("p");
      date.textContent = `Iniciada em ${new Date(election.createdAt).toLocaleDateString("pt-BR")}`;
      const resultList = document.createElement("ul");
      resultList.className = "result-list";
      election.results.candidates.forEach((candidate) => {
        const result = document.createElement("li");
        const name = document.createElement("span");
        const votes = document.createElement("strong");
        name.textContent = candidate.name;
        votes.textContent = `${candidate.votes} voto(s)`;
        result.append(name, votes);
        resultList.append(result);
      });
      const blankResult = document.createElement("li");
      blankResult.append(document.createTextNode("Votos em branco"));
      const blankVotes = document.createElement("strong");
      blankVotes.textContent = `${election.results.blankVotes} voto(s)`;
      blankResult.append(blankVotes);
      resultList.append(blankResult);
      const summary = document.createElement("p");
      summary.textContent =
        `Votos registrados: ${election.results.castVotes} · Participação: ${election.results.turnout} de ${election.results.eligibleVoters} eleitores.`;
      item.append(heading, date, resultList, summary);
      historyList.append(item);
    });
  }

  async function logout() {
    try {
      await api("/api/logout", { method: "POST", payload: {} });
    } catch (error) {
      const messageId = $("#adminView").classList.contains("hidden")
        ? $("#employeeView").classList.contains("hidden")
          ? "employeeLoginMessage"
          : "employeeLogoutMessage"
        : "adminLoginMessage";
      showMessage(messageId, `Não foi possível encerrar a sessão no servidor: ${error.message}`);
      return;
    }
    $("#employeeLoginForm").reset();
    $("#adminLoginForm").reset();
    $("#adminSetupForm").reset();
    $('[data-role="employee"]').click();
    showView("loginView");
  }

  function selectRole(role) {
    const isEmployee = role === "employee";
    document.querySelectorAll("[data-role]").forEach((item) => {
      item.setAttribute("aria-pressed", String(item.dataset.role === role));
    });
    $("#employeeLoginForm").classList.toggle("hidden", !isEmployee);
    $("#adminLoginForm").classList.toggle("hidden", isEmployee || adminSetupRequired);
    $("#adminSetupForm").classList.toggle("hidden", isEmployee || !adminSetupRequired);
    showMessage("employeeLoginMessage", "");
    showMessage("adminLoginMessage", "");
    showMessage("adminSetupMessage", "");
  }

  document.querySelectorAll("[data-role]").forEach((button) => {
    button.addEventListener("click", () => {
      selectRole(button.dataset.role);
    });
  });

  $("#employeeLoginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const session = await api("/api/login", {
        method: "POST",
        payload: {
          role: "employee",
          registration: data.get("registration"),
          code: data.get("code"),
        },
      });
      $("#employeeName").textContent = session.name;
      showView("employeeView");
    } catch (error) {
      showMessage("employeeLoginMessage", error.message);
    }
  });

  $("#adminLoginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await api("/api/login", {
        method: "POST",
        payload: { role: "admin", email: data.get("email"), password: data.get("password") },
      });
      await renderAdmin();
      showView("adminView");
    } catch (error) {
      showMessage("adminLoginMessage", error.message);
    }
  });

  $("#adminSetupForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const password = data.get("password");
    if (password !== data.get("passwordConfirm")) {
      showMessage("adminSetupMessage", "As senhas não coincidem.");
      return;
    }
    try {
      await api("/api/admin/setup", {
        method: "POST",
        payload: {
          name: data.get("name"),
          email: data.get("email"),
          password,
        },
      });
      adminSetupRequired = false;
      await renderAdmin();
      showView("adminView");
    } catch (error) {
      showMessage("adminSetupMessage", error.message);
    }
  });

  $("#employeeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const employee = await api("/api/admin/employees", {
        method: "POST",
        payload: {
          name: data.get("name"),
          registration: data.get("registration"),
          sector: data.get("sector"),
        },
      });
      form.reset();
      $("#employeeCredential").replaceChildren(
        document.createTextNode(`Código de acesso de ${employee.name} — entregue ao eleitor e guarde-o em segurança. Ele não será exibido novamente.`),
      );
      const code = document.createElement("strong");
      code.textContent = employee.code;
      $("#employeeCredential").append(code);
      showMessage("employeeFormMessage", "Eleitor cadastrado. O código só será mostrado agora.");
      await renderAdmin();
    } catch (error) {
      showMessage("employeeFormMessage", error.message);
    }
  });

  $("#candidateForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await api("/api/admin/candidates", {
        method: "POST",
        payload: {
          name: data.get("name"),
          role: data.get("role"),
          pitch: data.get("pitch"),
        },
      });
      form.reset();
      showMessage("candidateFormMessage", "Candidatura cadastrada.");
      await renderAdmin();
    } catch (error) {
      showMessage("candidateFormMessage", error.message);
    }
  });

  $("#openElectionButton").addEventListener("click", async () => {
    if (!window.confirm("Abrir a votação? Os cadastros serão bloqueados e não poderão ser alterados.")) return;
    try {
      await api("/api/admin/election/open", { method: "POST", payload: {} });
      await renderAdmin();
    } catch (error) {
      showMessage("electionControlMessage", error.message);
    }
  });

  $("#closeElectionButton").addEventListener("click", async () => {
    if (!window.confirm("Encerrar definitivamente a votação? Esta ação não pode ser desfeita.")) return;
    try {
      await api("/api/admin/election/close", { method: "POST", payload: {} });
      await renderAdmin();
    } catch (error) {
      showMessage("electionControlMessage", error.message);
    }
  });

  $("#newElectionButton").addEventListener("click", async () => {
    if (!window.confirm("Iniciar uma nova eleição? Os cadastros começarão vazios e os resultados anteriores serão preservados no histórico.")) return;
    try {
      await api("/api/admin/election/new", { method: "POST", payload: {} });
      $("#employeeCredential").replaceChildren();
      showMessage("employeeFormMessage", "");
      showMessage("candidateFormMessage", "");
      await renderAdmin();
    } catch (error) {
      showMessage("electionControlMessage", error.message);
    }
  });

  $("#logoutButton").addEventListener("click", logout);
  $("#employeeLogout").addEventListener("click", logout);

  (async () => {
    try {
      const setup = await api("/api/setup");
      adminSetupRequired = setup.setupRequired;
      if (adminSetupRequired) selectRole("admin");
      const session = await api("/api/session");
      if (!session.authenticated) return;
      if (session.role === "admin") {
        await renderAdmin();
        showView("adminView");
      } else {
        $("#employeeName").textContent = session.name;
        showView("employeeView");
      }
    } catch (error) {
      showMessage("adminLoginMessage", error.message);
    }
  })();
})();
