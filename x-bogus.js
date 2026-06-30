/**
 * Douyin a_bogus / x-bogus Signature Generator
 *
 * Generates the signature parameter required for Douyin API requests.
 * Based on the open-source DouyinLiveRecorder project analysis.
 *
 * Usage: node x-bogus.js "<url_params>" "<user_agent>"
 * Output: JSON { "a_bogus": "...", "msToken": "..." }
 *
 * Reference: https://github.com/ihmily/DouyinLiveRecorder
 */

const crypto = require('crypto');

// Douyin custom Base64 alphabet
const BASE64_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=";

// Standard RC4 encryption
function rc4(key, data) {
    const keyBytes = typeof key === 'string' ? Buffer.from(key, 'utf8') : Buffer.from(key);
    const dataBytes = typeof data === 'string' ? Buffer.from(data, 'utf8') : Buffer.from(data);

    const S = Array.from({ length: 256 }, (_, i) => i);
    let j = 0;

    for (let i = 0; i < 256; i++) {
        j = (j + S[i] + keyBytes[i % keyBytes.length]) & 0xFF;
        [S[i], S[j]] = [S[j], S[i]];
    }

    let i = 0;
    j = 0;
    const result = Buffer.alloc(dataBytes.length);

    for (let k = 0; k < dataBytes.length; k++) {
        i = (i + 1) & 0xFF;
        j = (j + S[i]) & 0xFF;
        [S[i], S[j]] = [S[j], S[i]];
        result[k] = dataBytes[k] ^ S[(S[i] + S[j]) & 0xFF];
    }

    return result;
}

// MD5 hash and return hex byte array
function md5ToBytes(data) {
    const hash = crypto.createHash('md5').update(data).digest('hex');
    const bytes = [];
    for (let i = 0; i < hash.length; i += 2) {
        bytes.push(parseInt(hash.substring(i, i + 2), 16));
    }
    return bytes;
}

// Custom base64 encode using Douyin alphabet
function customBase64Encode(bytes) {
    let result = '';
    const len = bytes.length;

    for (let i = 0; i < len; i += 3) {
        const b1 = bytes[i];
        const b2 = i + 1 < len ? bytes[i + 1] : 0;
        const b3 = i + 2 < len ? bytes[i + 2] : 0;

        const combined = (b1 << 16) | (b2 << 8) | b3;

        result += BASE64_ALPHABET.charAt((combined >> 18) & 0x3F);
        result += BASE64_ALPHABET.charAt((combined >> 12) & 0x3F);

        if (i + 1 < len) {
            result += BASE64_ALPHABET.charAt((combined >> 6) & 0x3F);
        }
        if (i + 2 < len) {
            result += BASE64_ALPHABET.charAt(combined & 0x3F);
        }
    }

    return result;
}

// Generate the 19-element array
function generateArray(md5Params1, md5Params2, md5UA, timestamp) {
    const cvs = 536919696; // Fixed canvas fingerprint

    const arr = [
        64,                    // [0] 固定
        1,                     // [1] 固定
        md5Params1[14],        // [2]
        md5Params2[14],        // [3]
        69,                    // [4] 'E'
        98,                    // [5] 'b'
        (timestamp >> 8) & 255,// [6] 时间戳字节1
        (cvs >> 24) & 255,     // [7] canvas字节3
        77,                    // [8] 'M'
        0.00390625,            // [9] 固定值
        8,                     // [10] 固定
        124,                   // [11] '|'
        md5UA[14],             // [12]
        md5Params1[15],        // [13]
        md5Params2[15],        // [14]
        (timestamp >> 16) & 255,// [15] 时间戳字节2
        timestamp & 255,       // [16] 时间戳字节0
        (cvs >> 16) & 255,     // [17] canvas字节1
        cvs & 255              // [18] canvas字节0
    ];

    return arr;
}

// Convert 19-element array to string bytes
function arrayToString(arr) {
    const bytes = [];
    for (let i = 0; i < arr.length; i++) {
        if (typeof arr[i] === 'number') {
            if (Number.isInteger(arr[i])) {
                bytes.push(arr[i] & 0xFF);
            } else {
                // Float value - convert to byte representation
                bytes.push(Math.floor(arr[i] * 256) & 0xFF);
            }
        }
    }
    return Buffer.from(bytes);
}

/**
 * Generate a_bogus signature
 * @param {string} params - URL query string (e.g. "aid=6383&sec_user_id=...")
 * @param {string} userAgent - Browser User-Agent string
 * @returns {string} The a_bogus signature
 */
function generateABogus(params, userAgent) {
    const timestamp = Math.floor(Date.now() / 1000);

    // Double MD5 of params
    const md5_1 = md5ToBytes(params);
    const md5_2 = md5ToBytes(Buffer.from(md5_1));

    // Double MD5 of empty body
    const bodyMd5_1 = md5ToBytes('');
    const bodyMd5_2 = md5ToBytes(Buffer.from(bodyMd5_1));

    // RC4 encrypt UA, custom base64, then MD5
    const uaKey = Buffer.from([0, 1, 14]);
    const uaEncrypted = rc4(uaKey, userAgent);
    const uaBase64 = customBase64Encode(uaEncrypted);
    const uaMd5 = md5ToBytes(uaBase64);

    // Generate 19-element array
    const arr19 = generateArray(md5_1, md5_2, uaMd5, timestamp);

    // Convert to string bytes and RC4 encrypt
    const arrBytes = arrayToString(arr19);
    const rc4Key = Buffer.from([0xFF]);  // ÿ
    const encrypted = rc4(rc4Key, arrBytes);

    // Prepend \x02\xFF (STX + ÿ)
    const prefix = Buffer.from([0x02, 0xFF]);
    const finalBytes = Buffer.concat([prefix, encrypted]);

    // Encode using custom base64
    return customBase64Encode(finalBytes);
}

// Generate a random msToken (UUID-like)
function generateMsToken() {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    let token = '';
    for (let i = 0; i < 128; i++) {
        token += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return token;
}

// ====== CLI Entry Point ======
const args = process.argv.slice(2);
if (args.length < 1) {
    console.log(JSON.stringify({
        usage: "node x-bogus.js '<url_params>' '<user_agent>'",
        example: 'node x-bogus.js "aid=6383&sec_user_id=xxx" "Mozilla/5.0..."'
    }));
    process.exit(1);
}

const params = args[0];
const ua = args[1] || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

try {
    const aBogus = generateABogus(params, ua);
    const msToken = generateMsToken();

    console.log(JSON.stringify({
        a_bogus: aBogus,
        msToken: msToken
    }));
} catch (error) {
    console.error(JSON.stringify({
        error: error.message
    }));
    process.exit(1);
}
