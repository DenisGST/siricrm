module.exports = {
  content: [
    "./templates/**/*.html",
    "./**/templates/**/*.html",
    "./static/js/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
      },
    },
  },
  plugins: [require("daisyui")],
  daisyui: {
  themes: [
    {
      siricrm: {
        "primary":          "#0079d3",   // синие кнопки
        "primary-content":  "#ffffff",   // текст на синих кнопках
        "secondary":        "#6890b9",   // акцент
        "accent":           "#0545ac",
        "neutral":          "#1a1a1b",
        "base-100":         "#ffffff",   // фон карточек
        "base-200":         "#f6f7f8",   // фон сайдбара
        "base-300":         "#edeff1",   // границы
        "base-content":     "#1a1a1b",   // основной текст
        "info":             "#e9f5ff",
        "success":          "#36d399",
        "warning":          "#fbbd23",
        "error":            "#f87171",
      },
    },
  ],
},
};
