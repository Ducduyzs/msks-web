// Trích mọi câu giao diện trong t('…') / msg('…') → src/i18n/catalog.json (đầu vào của scripts/translate_ui.py).
//   npm run i18n:extract            # ghi catalog
//   npm run i18n:extract -- --check # báo lỗi nếu catalog lỗi thời hoặc locale thiếu câu (dùng trong CI)
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = path.join(web, 'src')
const catalogPath = path.join(src, 'i18n', 'catalog.json')
const SKIP = /(\.test\.|[\\/]api[\\/]mock|vite-env\.d\.ts)/

const messages = new Set()
const where = new Map() // câu → các file dùng nó (ngữ cảnh cho người/máy dịch)
const dynamic = []

function scan(file) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const visit = (node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && ['t', 'msg'].includes(node.expression.text) && node.arguments.length) {
      const arg = node.arguments[0]
      if (ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg)) {
        messages.add(arg.text)
        const at = path.relative(src, file).split(path.sep).join('/')
        where.set(arg.text, [...new Set([...(where.get(arg.text) ?? []), at])])
      }
      else if (!file.includes(`${path.sep}i18n${path.sep}`)) {
        dynamic.push(`${path.relative(web, file)}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`)
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
}

;(function walk(dir) {
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name)
    if (fs.statSync(p).isDirectory()) walk(p)
    else if (/\.(ts|tsx)$/.test(name) && !SKIP.test(p)) scan(p)
  }
})(src)

const catalog = [...messages].sort((a, b) => a.localeCompare(b, 'vi'))
const context = Object.fromEntries(catalog.map((m) => [m, where.get(m)]))
const serialized = JSON.stringify({ source: 'vi', messages: catalog, where: context }, null, 2) + '\n'

if (process.argv.includes('--check')) {
  const current = fs.existsSync(catalogPath) ? fs.readFileSync(catalogPath, 'utf8') : ''
  let failed = current !== serialized
  if (failed) console.error('catalog.json lỗi thời: chạy npm run i18n:extract')
  for (const file of fs.readdirSync(path.join(src, 'i18n', 'locales'))) {
    const dict = JSON.parse(fs.readFileSync(path.join(src, 'i18n', 'locales', file), 'utf8'))
    const missing = catalog.filter((m) => !(m in dict))
    if (missing.length) {
      failed = true
      console.error(`${file}: thiếu ${missing.length} câu (chạy python scripts/translate_ui.py)`)
    }
  }
  process.exit(failed ? 1 : 0)
}
fs.writeFileSync(catalogPath, serialized)
console.log(`${catalog.length} câu → ${path.relative(web, catalogPath)}`)
if (dynamic.length) console.log(`t()/msg() với tham số không phải chuỗi (bảng nhãn dịch qua translated): ${dynamic.length}`)
