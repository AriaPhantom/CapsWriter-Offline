/// <reference types="vite/client" />

// CSS 以副作用方式导入，仅需让 TS 知道这些模块存在
declare module "*.css";
