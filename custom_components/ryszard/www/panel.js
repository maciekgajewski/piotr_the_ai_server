class RyszardPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._loadSettings();
    }
  }

  connectedCallback() {
    this.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100vh;
          box-sizing: border-box;
          padding: 24px;
          color: var(--primary-text-color);
          background: var(--primary-background-color);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }
        .shell {
          max-width: 900px;
          margin: 0 auto;
        }
        h1 {
          margin: 0 0 20px;
          font-size: 28px;
          font-weight: 500;
        }
        table {
          width: 100%;
          border-collapse: collapse;
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
        }
        th, td {
          padding: 10px;
          border-bottom: 1px solid var(--divider-color);
          text-align: left;
        }
        input {
          width: 100%;
          box-sizing: border-box;
          padding: 8px;
          color: var(--primary-text-color);
          background: var(--secondary-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 4px;
        }
        button {
          min-height: 36px;
          padding: 0 14px;
          border: 0;
          border-radius: 4px;
          color: var(--text-primary-color);
          background: var(--primary-color);
          cursor: pointer;
        }
        button.secondary {
          color: var(--primary-text-color);
          background: var(--secondary-background-color);
          border: 1px solid var(--divider-color);
        }
        .actions {
          display: flex;
          gap: 8px;
          margin-top: 16px;
          align-items: center;
        }
        .status {
          color: var(--secondary-text-color);
        }
      </style>
      <div class="shell">
        <h1>Ryszard</h1>
        <table>
          <thead>
            <tr>
              <th>Alias</th>
              <th>Playlist search text</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="aliases"></tbody>
        </table>
        <div class="actions">
          <button class="secondary" id="add">Add alias</button>
          <button id="save">Save</button>
          <span class="status" id="status"></span>
        </div>
      </div>
    `;
    this.querySelector("#add").addEventListener("click", () => this._addRow("", ""));
    this.querySelector("#save").addEventListener("click", () => this._saveSettings());
  }

  async _loadSettings() {
    if (!this._hass) {
      return;
    }
    this._setStatus("Loading...");
    const result = await this._hass.callWS({ type: "ryszard/settings/get" });
    const aliases = result.settings?.media?.playlist_aliases || {};
    this.querySelector("#aliases").innerHTML = "";
    Object.entries(aliases).forEach(([alias, target]) => this._addRow(alias, target));
    if (!Object.keys(aliases).length) {
      this._addRow("", "");
    }
    this._setStatus("");
  }

  _addRow(alias, target) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input class="alias" value=""></td>
      <td><input class="target" value=""></td>
      <td><button class="secondary remove">Remove</button></td>
    `;
    row.querySelector(".alias").value = alias;
    row.querySelector(".target").value = target;
    row.querySelector(".remove").addEventListener("click", () => row.remove());
    this.querySelector("#aliases").append(row);
  }

  async _saveSettings() {
    const playlistAliases = {};
    this.querySelectorAll("#aliases tr").forEach((row) => {
      const alias = row.querySelector(".alias").value.trim();
      const target = row.querySelector(".target").value.trim();
      if (alias && target) {
        playlistAliases[alias] = target;
      }
    });
    this._setStatus("Saving...");
    await this._hass.callWS({
      type: "ryszard/settings/update",
      settings: { media: { playlist_aliases: playlistAliases } },
    });
    this._setStatus("Saved");
  }

  _setStatus(text) {
    const status = this.querySelector("#status");
    if (status) {
      status.textContent = text;
    }
  }
}

customElements.define("ryszard-panel", RyszardPanel);
