(function () {
function normalize(text) {
    return (text || "")
        .toString()
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .trim();
}

function enhanceNationalityAsDropdown() {
    const questionBlocks = document.querySelectorAll(
        ".o_survey_answer_wrapper[data-question-type='simple_choice_radio']"
    );
    questionBlocks.forEach((block) => {
        if (block.dataset.ghrDropdownApplied === "1") {
            return;
        }

        const questionContainer = block.closest(".o_survey_question");
        const questionTitleEl = questionContainer
            ? questionContainer.querySelector(".o_survey_question_title")
            : null;
        const questionTitle = normalize(questionTitleEl ? questionTitleEl.textContent : "");

        // Aplicar solo a la pregunta de Nacionalidad.
        if (!questionTitle.includes("nacionalidad")) {
            return;
        }

        const radios = Array.from(
            block.querySelectorAll("input.o_survey_form_choice_item[type='radio']")
        );
        if (!radios.length) {
            return;
        }

        const selectWrap = document.createElement("div");
        selectWrap.className = "ghr-survey-select-wrap mt-1 mb-2";

        const select = document.createElement("select");
        select.className = "form-select ghr-survey-select";
        select.setAttribute("aria-label", "Nacionalidad");

        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Seleccione una opción";
        select.appendChild(placeholder);

        let hasSelected = false;
        radios.forEach((radio) => {
            const label = block.querySelector(`label[for='${radio.id}']`);
            if (!label) {
                return;
            }
            const textSpan = label.querySelector("span.ms-2.text-break");
            const labelText = (textSpan ? textSpan.textContent : label.textContent || "").trim();
            if (!labelText) {
                return;
            }

            const opt = document.createElement("option");
            opt.value = radio.value;
            opt.textContent = labelText;
            if (radio.checked) {
                opt.selected = true;
                hasSelected = true;
            }
            select.appendChild(opt);
        });

        if (!hasSelected) {
            select.value = "";
        }

        select.addEventListener("change", () => {
            const selectedValue = select.value;
            radios.forEach((radio) => {
                const checked = selectedValue && radio.value === selectedValue;
                radio.checked = !!checked;
                if (checked) {
                    radio.dispatchEvent(new Event("change", { bubbles: true }));
                    radio.dispatchEvent(new Event("input", { bubbles: true }));
                }
            });
        });

        selectWrap.appendChild(select);
        block.prepend(selectWrap);

        // Ocultar lista de radios, dejando activo solo el select.
        const radioRows = block.querySelectorAll(".col-sm-12");
        radioRows.forEach((row) => {
            row.style.display = "none";
        });

        block.dataset.ghrDropdownApplied = "1";
    });
}

function boot() {
    enhanceNationalityAsDropdown();
    const observer = new MutationObserver(() => enhanceNationalityAsDropdown());
    observer.observe(document.body, { childList: true, subtree: true });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
} else {
    boot();
}
})();
