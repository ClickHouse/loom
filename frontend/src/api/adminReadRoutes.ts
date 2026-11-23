// These operator reads have dedicated HTTP handlers outside the operation registry.
export const ADMIN_SETTINGS_READ_ROUTES = Object.freeze({
  list: "/v1/admin/settings.list",
  history: "/v1/admin/settings.history",
});
