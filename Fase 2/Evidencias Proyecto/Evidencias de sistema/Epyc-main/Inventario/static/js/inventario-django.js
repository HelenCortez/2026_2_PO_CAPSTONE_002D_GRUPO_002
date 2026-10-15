document.addEventListener('DOMContentLoaded', () => {
  // Pide confirmación antes de eliminar un producto
  document.querySelectorAll('form[data-confirm-delete]').forEach((formEliminar) => {
    formEliminar.addEventListener('submit', async (e) => {
      if (formEliminar.dataset.confirmado) return;
      e.preventDefault();

      const nombre = formEliminar.dataset.confirmDelete.trim() || 'este producto';
      const opciones = {
        titulo: 'Eliminar producto',
        mensaje: `¿Seguro que quieres eliminar ${nombre}? Esta acción no se puede deshacer.`,
        textoConfirmar: 'Eliminar',
        peligroso: true,
      };

      const ok = typeof confirmarAccion === 'function'
        ? await confirmarAccion(opciones)
        : window.confirm(opciones.mensaje);

      if (ok) {
        formEliminar.dataset.confirmado = '1';
        formEliminar.submit();
      }
    });
  });

  const form = document.getElementById('inventario-filter-form');
  const searchInput = document.getElementById('inv-search');
  const categoriaSelect = document.getElementById('inv-categoria');
  const tbody = document.getElementById('inv-tbody');

  if (!form || !searchInput || !categoriaSelect || !tbody) return;

  const rows = [...tbody.querySelectorAll('tr[data-producto]')];

  const normalize = (value) => (value || '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .trim();

  const emptyRow = document.createElement('tr');
  emptyRow.hidden = true;
  emptyRow.innerHTML = '<td colspan="8" class="empty-state">No encontramos productos con ese criterio.</td>';
  tbody.append(emptyRow);

  function applyFilters() {
    const term = normalize(searchInput.value);
    const categoria = categoriaSelect.value;
    let visibles = 0;

    rows.forEach((row) => {
      const matchesText = !term
        || normalize(row.dataset.nombre).includes(term)
        || normalize(row.dataset.codigo).includes(term);
      const matchesCategoria = !categoria || row.dataset.categoria === categoria;
      row.hidden = !(matchesText && matchesCategoria);
      if (!row.hidden) visibles += 1;
    });

    emptyRow.hidden = visibles > 0 || rows.length === 0;
    return visibles;
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const visibles = applyFilters();
    if (typeof mostrarToast === 'function') {
      mostrarToast(`Búsqueda realizada: ${visibles} producto${visibles === 1 ? '' : 's'} encontrado${visibles === 1 ? '' : 's'}.`);
    }
  });

  form.addEventListener('reset', () => {
    window.setTimeout(applyFilters, 0);
  });

  const categoriaURL = new URLSearchParams(window.location.search).get('categoria');
  if (categoriaURL) categoriaSelect.value = categoriaURL;

  applyFilters();
});
