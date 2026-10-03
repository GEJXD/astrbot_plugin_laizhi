const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;

const state = {
  tags: [],
  selectedTag: null,
  tagQuery: "",
  fileQuery: "",
  page: 1,
  pageSize: 50,
  totalFiles: 0,
};

const elements = {
  notice: document.getElementById("notice"),
  tagTotal: document.getElementById("tag-total"),
  tagList: document.getElementById("tag-list"),
  tagSearch: document.getElementById("tag-search"),
  createTagForm: document.getElementById("create-tag-form"),
  newTagName: document.getElementById("new-tag-name"),
  emptyState: document.getElementById("empty-state"),
  tagDetail: document.getElementById("tag-detail"),
  selectedTagName: document.getElementById("selected-tag-name"),
  selectedTagMeta: document.getElementById("selected-tag-meta"),
  renameTag: document.getElementById("rename-tag"),
  deleteTag: document.getElementById("delete-tag"),
  fileUpload: document.getElementById("file-upload"),
  fileSearch: document.getElementById("file-search"),
  refreshFiles: document.getElementById("refresh-files"),
  fileList: document.getElementById("file-list"),
  previousPage: document.getElementById("previous-page"),
  nextPage: document.getElementById("next-page"),
  pageInfo: document.getElementById("page-info"),
  refreshAll: document.getElementById("refresh-all"),
};

function unwrap(value) {
  if (value && value.status === "ok" && Object.prototype.hasOwnProperty.call(value, "data")) {
    return value.data;
  }
  return value;
}

function showNotice(message, type = "success") {
  elements.notice.textContent = message || "";
  elements.notice.className = message ? `notice ${type}` : "notice";
}

function showError(error) {
  showNotice(error?.message || String(error) || "操作失败", "error");
}

function formatBytes(value) {
  const size = Number(value) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value) {
  if (!value) return "未知时间";
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString();
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function button(text, className, onClick) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = `button ${className}`;
  item.textContent = text;
  item.addEventListener("click", onClick);
  return item;
}

function renderTags() {
  clear(elements.tagList);
  const query = state.tagQuery.trim().toLocaleLowerCase();
  const visible = state.tags.filter((tag) => {
    if (!query) return true;
    return `${tag.name} ${tag.effective_name}`.toLocaleLowerCase().includes(query);
  });
  elements.tagTotal.textContent = `${state.tags.length}`;

  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty-list";
    empty.textContent = state.tags.length ? "没有匹配的标签" : "还没有标签";
    elements.tagList.appendChild(empty);
    return;
  }

  for (const tag of visible) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `tag-item${state.selectedTag?.id === tag.id ? " active" : ""}`;
    item.addEventListener("click", () => selectTag(tag));

    const name = document.createElement("span");
    name.className = "tag-item-name";
    name.textContent = tag.name;
    const count = document.createElement("span");
    count.className = "tag-count";
    count.textContent = `${tag.count}`;
    item.append(name, count);

    if (tag.is_alias) {
      const alias = document.createElement("small");
      alias.className = "tag-alias";
      alias.textContent = `别名 → ${tag.effective_name}`;
      item.appendChild(alias);
    }
    elements.tagList.appendChild(item);
  }
}

function renderSelectedTag() {
  const tag = state.selectedTag;
  const hasTag = Boolean(tag);
  elements.emptyState.classList.toggle("hidden", hasTag);
  elements.tagDetail.classList.toggle("hidden", !hasTag);
  if (!tag) return;

  elements.selectedTagName.textContent = tag.name;
  elements.selectedTagMeta.textContent = tag.is_alias
    ? `${tag.count} 个内容 · 别名指向「${tag.effective_name}」`
    : `${tag.count} 个内容`;
}

function renderFiles(files) {
  clear(elements.fileList);
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state compact-empty";
    const title = document.createElement("h3");
    title.textContent = state.fileQuery ? "没有匹配内容" : "这个标签还没有内容";
    const text = document.createElement("p");
    text.textContent = state.fileQuery ? "换一个关键词试试。" : "点击上方「上传内容」添加图片、视频或音频。";
    empty.append(title, text);
    elements.fileList.appendChild(empty);
    return;
  }

  for (const file of files) {
    const card = document.createElement("article");
    card.className = "file-card";
    const main = document.createElement("div");
    main.className = "file-main";
    const title = document.createElement("strong");
    title.textContent = `${file.kind} · .${file.ext}`;
    const details = document.createElement("p");
    details.className = "file-details";
    details.textContent = `${formatBytes(file.size)} · ${formatDate(file.added_at)} · ${file.hash.slice(0, 16)}…`;
    main.append(title, details);

    const actions = document.createElement("div");
    actions.className = "file-actions";
    actions.appendChild(
      button("下载", "secondary", async () => {
        try {
          await bridge.download(file.download_endpoint, {}, `laizhi_${file.hash.slice(0, 12)}.${file.ext}`);
        } catch (error) {
          showError(error);
        }
      }),
    );
    actions.appendChild(
      button("移出标签", "danger subtle-danger", async () => {
        if (!window.confirm(`确定从「${state.selectedTag.name}」移出这份内容吗？`)) return;
        try {
          await bridge.apiPost(`tags/${state.selectedTag.id}/files/${file.id}/detach`, {});
          showNotice("内容已移出标签");
          await Promise.all([loadTags(false), loadFiles()]);
        } catch (error) {
          showError(error);
        }
      }),
    );
    card.append(main, actions);
    elements.fileList.appendChild(card);
  }
}

function updatePagination() {
  const pages = Math.max(1, Math.ceil(state.totalFiles / state.pageSize));
  elements.pageInfo.textContent = `第 ${state.page} / ${pages} 页 · 共 ${state.totalFiles} 个`;
  elements.previousPage.disabled = state.page <= 1;
  elements.nextPage.disabled = state.page >= pages;
}

async function loadTags(selectFirst = true) {
  const response = unwrap(await bridge.apiGet("tags", { limit: 1000 }));
  state.tags = Array.isArray(response?.tags) ? response.tags : [];
  if (state.selectedTag) {
    state.selectedTag = state.tags.find((tag) => tag.id === state.selectedTag.id) || null;
  }
  if (!state.selectedTag && selectFirst && state.tags.length) {
    state.selectedTag = state.tags[0];
  }
  renderTags();
  renderSelectedTag();
}

async function loadFiles() {
  if (!state.selectedTag) {
    renderFiles([]);
    updatePagination();
    return;
  }
  const response = unwrap(
    await bridge.apiGet(`tags/${state.selectedTag.id}/files`, {
      page: state.page,
      limit: state.pageSize,
      q: state.fileQuery,
    }),
  );
  state.totalFiles = Number(response?.total) || 0;
  state.selectedTag = response?.tag || state.selectedTag;
  renderSelectedTag();
  renderFiles(Array.isArray(response?.files) ? response.files : []);
  updatePagination();
}

async function selectTag(tag) {
  state.selectedTag = tag;
  state.page = 1;
  state.fileQuery = "";
  elements.fileSearch.value = "";
  renderTags();
  renderSelectedTag();
  try {
    await loadFiles();
  } catch (error) {
    showError(error);
  }
}

async function refreshAll() {
  try {
    await loadTags(false);
    await loadFiles();
    showNotice("已刷新");
  } catch (error) {
    showError(error);
  }
}

elements.createTagForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = elements.newTagName.value.trim();
  if (!name) return;
  try {
    const response = unwrap(await bridge.apiPost("tags", { name }));
    elements.newTagName.value = "";
    await loadTags(false);
    const created = response?.tag;
    if (created) await selectTag(created);
    showNotice(response?.created === false ? "标签已存在" : "标签已创建");
  } catch (error) {
    showError(error);
  }
});

elements.tagSearch.addEventListener("input", (event) => {
  state.tagQuery = event.target.value;
  renderTags();
});

elements.fileSearch.addEventListener("input", async (event) => {
  state.fileQuery = event.target.value;
  state.page = 1;
  try {
    await loadFiles();
  } catch (error) {
    showError(error);
  }
});

elements.refreshFiles.addEventListener("click", async () => {
  try {
    await loadFiles();
    showNotice("内容已刷新");
  } catch (error) {
    showError(error);
  }
});

elements.refreshAll.addEventListener("click", refreshAll);

elements.fileUpload.addEventListener("change", async () => {
  const file = elements.fileUpload.files?.[0];
  if (!file || !state.selectedTag) return;
  try {
    await bridge.upload(`tags/${state.selectedTag.id}/files`, file);
    elements.fileUpload.value = "";
    showNotice("内容已上传");
    await Promise.all([loadTags(false), loadFiles()]);
  } catch (error) {
    elements.fileUpload.value = "";
    showError(error);
  }
});

elements.renameTag.addEventListener("click", async () => {
  if (!state.selectedTag) return;
  const name = window.prompt("请输入新的标签名", state.selectedTag.name);
  if (name === null || !name.trim()) return;
  try {
    await bridge.apiPost(`tags/${state.selectedTag.id}`, { name: name.trim() });
    showNotice("标签已重命名");
    await loadTags(false);
    await loadFiles();
  } catch (error) {
    showError(error);
  }
});

elements.deleteTag.addEventListener("click", async () => {
  if (!state.selectedTag) return;
  if (!window.confirm(`确定删除标签「${state.selectedTag.name}」吗？标签中的内容关系也会被移除。`)) return;
  try {
    await bridge.apiPost(`tags/${state.selectedTag.id}/delete`, {});
    state.selectedTag = null;
    showNotice("标签已删除");
    await loadTags(true);
    await loadFiles();
  } catch (error) {
    showError(error);
  }
});

elements.previousPage.addEventListener("click", async () => {
  if (state.page <= 1) return;
  state.page -= 1;
  try {
    await loadFiles();
  } catch (error) {
    showError(error);
  }
});

elements.nextPage.addEventListener("click", async () => {
  if (state.page >= Math.ceil(state.totalFiles / state.pageSize)) return;
  state.page += 1;
  try {
    await loadFiles();
  } catch (error) {
    showError(error);
  }
});

async function start() {
  if (!bridge) {
    showNotice("AstrBot WebUI bridge 不可用，请从插件详情页打开此页面。", "error");
    return;
  }
  try {
    await bridge.ready();
    await loadTags(true);
    await loadFiles();
  } catch (error) {
    showError(error);
  }
}

start();
