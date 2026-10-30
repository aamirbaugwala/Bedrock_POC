/* Webpack config for the Data Insights preview: misight's own dev config, with this folder's
 * preview.jsx as the entry. Run from the misight folder (see `make marketpro-preview`). */
const path = require("path");

const MISIGHT = path.resolve(__dirname, "../../../misight");
const local = require(path.join(MISIGHT, "web/webpack.local.js"));

// ESLintWebpackPlugin runs with fix: true in misight's config; it must not rewrite misight files.
const SKIPPED_PLUGINS = ["ESLintWebpackPlugin", "CleanTerminalPlugin", "CopyPlugin"];

module.exports = {
  ...local,
  entry: [path.join(__dirname, "preview.jsx")],
  output: { ...local.output, path: path.join(__dirname, ".build"), clean: true },
  module: {
    ...local.module,
    rules: local.module.rules.map((rule) =>
      rule.include ? { ...rule, include: [...rule.include, __dirname] } : rule,
    ),
  },
  plugins: local.plugins.filter((plugin) => !SKIPPED_PLUGINS.includes(plugin.constructor.name)),
  resolve: {
    ...local.resolve,
    alias: { ...local.resolve.alias, misight: MISIGHT },
    // Nested node_modules first (e.g. Redux Toolkit's own reselect), then misight's for this folder.
    modules: ["node_modules", path.join(MISIGHT, "node_modules")],
  },
  devServer: { port: 19007, server: "http", historyApiFallback: true, static: false },
};
