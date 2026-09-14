// Registers the .jsx loader for the test process. Used via `node --import`.
import { register } from 'node:module'
import { pathToFileURL } from 'node:url'
register('./jsx-loader.mjs', pathToFileURL(new URL('.', import.meta.url).pathname))
