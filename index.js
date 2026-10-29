(() => {
  const votingView = document.querySelector("#votingView");
  const receiptView = document.querySelector("#receiptView");
  const modal = document.querySelector("#confirmModal");
  const continueButton = document.querySelector("#continueButton");
  const selectionLabel = document.querySelector("#selectionLabel");
  const confirmChoice = document.querySelector("#confirmChoice");
  const candidatesElement = document.querySelector("#candidateList");
  let selectedId = null;
  let selectionText = "";

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

  function showReceipt(protocol) {
    votingView.classList.add("hidden");
    receiptView.classList.add("visible");
    document.querySelector("#protocolNumber").textContent = protocol;
  }

  function addCandidate(candidate) {
    const button = document.createElement("button");
    button.className = "candidate";
    button.type = "button";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", "false");
    button.dataset.vote = candidate.id;
    button.dataset.name = candidate.name;

    const number = document.createElement("span");
    number.className = "candidate-number";
    number.textContent = "Candidatura";
    const heading = document.createElement("h3");
    heading.textContent = candidate.name;
    const info = document.createElement("span");
    info.className = "candidate-info";
    const role = document.createElement("span");
    role.className = "candidate-role";
    role.textContent = candidate.role;
    const pitch = document.createElement("p");
    pitch.className = "candidate-pitch";
    pitch.textContent = candidate.pitch;
    info.append(number, heading, role, pitch);
    const radio = document.createElement("span");
    radio.className = "radio";
    radio.setAttribute("aria-hidden", "true");
    button.append(info, radio);
    candidatesElement.append(button);
    return button;
  }

  function select(option) {
    candidatesElement.querySelectorAll("[data-vote]").forEach((candidate) => {
      const isSelected = candidate === option;
      candidate.classList.toggle("selected", isSelected);
      candidate.setAttribute("aria-checked", String(isSelected));
    });
    const blankOption = document.querySelector(".blank-option");
    const isBlank = option === blankOption;
    blankOption.classList.toggle("selected", isBlank);
    blankOption.setAttribute("aria-checked", String(isBlank));
    selectedId = isBlank ? null : option.dataset.vote;
    selectionText = isBlank ? "Voto em branco" : option.dataset.name;
    selectionLabel.textContent = isBlank ? "Em branco" : selectionText;
    continueButton.disabled = false;
  }

  function showUnavailable(status) {
    votingView.classList.add("hidden");
    receiptView.classList.add("visible");
    document.querySelector("#receiptTitle").textContent =
      status === "closed" ? "Votação encerrada." : "Votação ainda não aberta.";
    document.querySelector("#receiptMessage").textContent =
      status === "closed"
        ? "A apuração será divulgada pela comissão eleitoral."
        : "A comissão eleitoral ainda está preparando a votação. Consulte o administrador.";
    document.querySelector("#protocolNumber").textContent = "";
    document.querySelector("#resetButton").classList.add("hidden");
    document.querySelector("#receiptProtocol").classList.add("hidden");
    document.querySelector("#receiptWarning").classList.add("hidden");
  }

  async function initialize() {
    try {
      const session = await api("/api/session");
      if (!session.authenticated || session.role !== "employee") {
        window.location.replace("acesso.html");
        return;
      }
      if (session.hasVoted) {
        document.querySelector("#receiptTitle").textContent = "Seu voto já foi registrado.";
        document.querySelector("#receiptMessage").textContent =
          "A urna confirmou um voto para esta matrícula. A escolha não está associada à sua identidade.";
        document.querySelector("#resetButton").classList.add("hidden");
        document.querySelector("#receiptProtocol").classList.add("hidden");
        document.querySelector("#receiptWarning").classList.add("hidden");
        showReceipt("");
        return;
      }

      const election = await api("/api/election");
      if (election.status !== "open") {
        showUnavailable(election.status);
        return;
      }
      candidatesElement.replaceChildren();
      election.candidates.forEach((candidate) => addCandidate(candidate));
      document.querySelector("#candidateCount").textContent =
        `${election.candidates.length} candidatura(s)`;
      document.querySelector(".blank-option").addEventListener("click", (event) => {
        select(event.currentTarget);
      });
      candidatesElement.querySelectorAll("[data-vote]").forEach((option, index, options) => {
        option.addEventListener("click", () => select(option));
        option.addEventListener("keydown", (event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowRight") {
            event.preventDefault();
            options[(index + 1) % options.length].focus();
          }
          if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
            event.preventDefault();
            options[(index - 1 + options.length) % options.length].focus();
          }
        });
      });
      document.querySelector("#electionStatus").textContent = "Votação aberta";
      continueButton.disabled = true;
    } catch (error) {
      document.querySelector("#voteError").textContent =
        `Não foi possível carregar a urna: ${error.message}`;
    }
  }

  continueButton.addEventListener("click", () => {
    if (!selectionText) return;
    confirmChoice.textContent = selectionText;
    modal.classList.add("open");
    document.querySelector("#confirmButton").focus();
  });

  document.querySelector("#cancelButton").addEventListener("click", () => {
    modal.classList.remove("open");
    continueButton.focus();
  });

  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      modal.classList.remove("open");
      continueButton.focus();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal.classList.contains("open")) {
      modal.classList.remove("open");
      continueButton.focus();
    }
  });

  document.querySelector("#confirmButton").addEventListener("click", async () => {
    const button = document.querySelector("#confirmButton");
    button.disabled = true;
    try {
      const receipt = await api("/api/vote", {
        method: "POST",
        payload: { candidateId: selectedId },
      });
      modal.classList.remove("open");
      document.querySelector("#receiptTitle").textContent = "Obrigada por participar.";
      document.querySelector("#receiptMessage").textContent =
        "Seu voto foi registrado no servidor. O comprovante não revela nem identifica sua escolha.";
      showReceipt(receipt.protocol);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      button.disabled = false;
      document.querySelector("#voteError").textContent = error.message;
    }
  });

  document.querySelector("#resetButton").addEventListener("click", () => {
    window.location.replace("acesso.html");
  });

  initialize();
})();
