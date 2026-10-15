package dev.pyxpo.modules.imagemanipulator

import dev.pyxpo.moduleapi.PxModule
import dev.pyxpo.moduleapi.PxModuleContext
import dev.pyxpo.moduleapi.PxReply
import org.json.JSONArray
import java.util.concurrent.Executors

class ImageManipulatorError(message: String) : Exception(message)

/** Native side of `pyxpo_image_manipulator`. Heavy work runs on one single-thread executor. */
class ImageManipulatorModule : PxModule() {
    override val name = "ImageManipulator"

    override fun call(ctx: PxModuleContext, method: String, args: JSONArray, reply: PxReply) {
        val cacheDir = ctx.activity.cacheDir
        when (method) {
            "ping" -> background(reply) { "pong" }
            "info" -> background(reply) { ImageManipulator.info(str(args, 0)) }
            "open" -> background(reply) { ImageManipulator.open(str(args, 0), if (args.length() > 1) args.optInt(1, 0) else 0) }
            "render" -> background(reply) {
                val spec = args.optJSONObject(1) ?: throw ImageManipulatorError("render: bad arguments")
                ImageManipulator.render(cacheDir, args.optInt(0, -1), spec)
            }
            "close" -> background(reply) { ImageManipulator.close(args.optInt(0, -1)); null }
            else -> reply(null, "unknown native call ImageManipulator.$method")
        }
    }

    companion object {
        private val executor = Executors.newSingleThreadExecutor { r -> Thread(r, "pyxpo-imagemanipulator") }

        fun str(args: JSONArray, i: Int): String {
            if (i >= args.length() || args.isNull(i)) throw ImageManipulatorError("missing string argument $i")
            return args.getString(i)
        }

        /** Accepts a plain path or a file:// URL. */
        fun path(uri: String): String = if (uri.startsWith("file://")) android.net.Uri.parse(uri).path ?: uri.substring(7) else uri

        private fun background(reply: PxReply, work: () -> Any?) {
            executor.execute {
                var value: Any? = null
                var message: String? = null
                try { value = work() }
                catch (e: ImageManipulatorError) { message = e.message }
                catch (e: Throwable) { message = e.toString() }
                reply(value, message)
            }
        }
    }
}

/**
 * Decode with BitmapFactory (EXIF orientation baked in), crop/resize with Bitmap, encode with Bitmap.compress.
 * Encoding writes no metadata, so output never carries EXIF/GPS. All state is touched on the media executor only.
 */
object ImageManipulator {
    private class Handle(val bitmap: android.graphics.Bitmap, val format: String)

    private val handles = HashMap<Int, Handle>()
    private var nextHandle = 1

    private fun formatOf(mime: String?): String = when (mime) {
        "image/jpeg" -> "jpeg"
        "image/png" -> "png"
        "image/heic", "image/heif" -> "heic"
        "image/gif" -> "gif"
        "image/webp" -> "webp"
        else -> "other"
    }

    private fun mimeOf(format: String) = if (format == "other") "application/octet-stream" else "image/$format"

    private fun bounds(path: String): android.graphics.BitmapFactory.Options {
        val o = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
        android.graphics.BitmapFactory.decodeFile(path, o)
        if (o.outWidth <= 0 || o.outHeight <= 0) throw ImageManipulatorError("cannot read image: ${java.io.File(path).name}")
        return o
    }

    private fun exif(path: String): android.media.ExifInterface? = try { android.media.ExifInterface(path) } catch (e: Exception) { null }

    private fun orientation(ex: android.media.ExifInterface?): Int =
        ex?.getAttributeInt(android.media.ExifInterface.TAG_ORIENTATION, android.media.ExifInterface.ORIENTATION_NORMAL) ?: 1

    fun info(uri: String): Map<String, Any?> {
        val path = ImageManipulatorModule.path(uri)
        val o = bounds(path)
        val ex = exif(path)
        val ori = orientation(ex)
        val swap = ori in 5..8
        val format = formatOf(o.outMimeType)
        val hasGps = ex?.getAttribute(android.media.ExifInterface.TAG_GPS_LATITUDE) != null
        val tags = listOf(android.media.ExifInterface.TAG_MAKE, android.media.ExifInterface.TAG_MODEL,
            android.media.ExifInterface.TAG_DATETIME, android.media.ExifInterface.TAG_USER_COMMENT,
            android.media.ExifInterface.TAG_SOFTWARE)
        val hasExif = hasGps || tags.any { ex?.getAttribute(it) != null }
        return mapOf("uri" to path, "width" to if (swap) o.outHeight else o.outWidth, "height" to if (swap) o.outWidth else o.outHeight,
            "fileSize" to java.io.File(path).length(), "format" to format, "mimeType" to mimeOf(format),
            "hasExif" to hasExif, "hasGps" to hasGps)
    }

    fun open(uri: String, maxEdge: Int): Map<String, Any?> {
        val path = ImageManipulatorModule.path(uri)
        val o = bounds(path)
        val ori = orientation(exif(path))
        val swap = ori in 5..8
        val sw = if (swap) o.outHeight else o.outWidth
        val sh = if (swap) o.outWidth else o.outHeight
        val longest = maxOf(sw, sh)
        val cap = if (maxEdge > 0) minOf(maxEdge, longest) else longest
        // Largest power-of-two subsample that stays within 10% of the cap (bounds memory on 48 MP photos).
        var sample = 1
        while (longest / (sample * 2) >= cap * 0.9) sample *= 2
        val opts = android.graphics.BitmapFactory.Options().apply { inSampleSize = sample }
        var bmp = android.graphics.BitmapFactory.decodeFile(path, opts) ?: throw ImageManipulatorError("cannot decode image")
        bmp = applyOrientation(bmp, ori)
        val big = maxOf(bmp.width, bmp.height)
        if (big > cap) bmp = scaled(bmp, maxOf(1, bmp.width * cap / big), maxOf(1, bmp.height * cap / big))
        val h = nextHandle++
        handles[h] = Handle(bmp, formatOf(o.outMimeType))
        return mapOf("h" to h, "width" to bmp.width, "height" to bmp.height, "sourceWidth" to sw, "sourceHeight" to sh)
    }

    private fun applyOrientation(src: android.graphics.Bitmap, ori: Int): android.graphics.Bitmap {
        val m = android.graphics.Matrix()
        when (ori) {
            2 -> m.postScale(-1f, 1f)
            3 -> m.postRotate(180f)
            4 -> m.postScale(1f, -1f)
            5 -> { m.postRotate(90f); m.postScale(-1f, 1f) }
            6 -> m.postRotate(90f)
            7 -> { m.postRotate(270f); m.postScale(-1f, 1f) }
            8 -> m.postRotate(270f)
            else -> return src
        }
        val out = android.graphics.Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
        if (out !== src) src.recycle()
        return out
    }

    /** Halves until within 2x of the target, then one filtered step, so big reductions do not alias. */
    private fun scaled(src: android.graphics.Bitmap, w: Int, h: Int): android.graphics.Bitmap {
        var cur = src
        while (cur.width / 2 >= w && cur.height / 2 >= h) {
            val next = android.graphics.Bitmap.createScaledBitmap(cur, cur.width / 2, cur.height / 2, true)
            if (cur !== src) cur.recycle()
            cur = next
        }
        if (cur.width == w && cur.height == h) return cur
        val out = android.graphics.Bitmap.createScaledBitmap(cur, w, h, true)
        if (cur !== src) cur.recycle()
        return out
    }

    /** One Expo-style action: crop, resize, rotate (multiples of 90, clockwise) or flip. */
    private fun apply(img: android.graphics.Bitmap, op: org.json.JSONObject): android.graphics.Bitmap = when (op.optString("op")) {
        "crop" -> {
            val x = maxOf(0, op.optInt("x")); val y = maxOf(0, op.optInt("y"))
            val w = minOf(op.optInt("width"), img.width - x); val h = minOf(op.optInt("height"), img.height - y)
            if (w <= 0 || h <= 0) throw ImageManipulatorError("crop outside image")
            android.graphics.Bitmap.createBitmap(img, x, y, w, h)
        }
        "resize" -> {
            val w = op.optInt("width"); val h = op.optInt("height")
            if (w < 1 || h < 1 || w > 16384 || h > 16384) throw ImageManipulatorError("bad output size ${w}x$h")
            scaled(img, w, h)
        }
        "rotate" -> {
            val d = ((op.optInt("degrees") % 360) + 360) % 360
            if (d % 90 != 0) throw ImageManipulatorError("rotate supports multiples of 90 degrees")
            if (d == 0) img else android.graphics.Bitmap.createBitmap(img, 0, 0, img.width, img.height,
                android.graphics.Matrix().apply { postRotate(d.toFloat()) }, true)
        }
        "flip" -> android.graphics.Bitmap.createBitmap(img, 0, 0, img.width, img.height,
            android.graphics.Matrix().apply { if (op.optString("axis") == "vertical") postScale(1f, -1f) else postScale(-1f, 1f) }, true)
        else -> throw ImageManipulatorError("unknown action")
    }

    fun render(cacheDir: java.io.File, h: Int, spec: org.json.JSONObject): Map<String, Any?> {
        val handle = handles[h] ?: throw ImageManipulatorError("image handle closed")
        var img = handle.bitmap
        var owned = false  // true once `img` is our own copy that we must recycle
        val ops = spec.optJSONArray("ops")
        if (ops != null) for (i in 0 until ops.length()) {
            val out = apply(img, ops.getJSONObject(i))
            if (owned && out !== img) img.recycle()
            if (out !== img) owned = true
            img = out
        }
        val crop = spec.optJSONObject("crop")
        if (crop != null) {
            val x = maxOf(0, crop.optInt("x")); val y = maxOf(0, crop.optInt("y"))
            val w = minOf(crop.optInt("width"), img.width - x); val ht = minOf(crop.optInt("height"), img.height - y)
            if (w <= 0 || ht <= 0) throw ImageManipulatorError("crop outside image")
            img = android.graphics.Bitmap.createBitmap(img, x, y, w, ht); owned = true
        }
        var format = spec.optString("format", "same")
        if (format == "same") format = if (handle.format == "jpeg" || handle.format == "png") handle.format else "jpeg"
        val cf = when (format) {
            "jpeg" -> android.graphics.Bitmap.CompressFormat.JPEG
            "png" -> android.graphics.Bitmap.CompressFormat.PNG
            "webp" -> if (android.os.Build.VERSION.SDK_INT >= 30) android.graphics.Bitmap.CompressFormat.WEBP_LOSSY else @Suppress("DEPRECATION") android.graphics.Bitmap.CompressFormat.WEBP
        "heic" -> throw ImageManipulatorError("HEIC output is not supported on Android")
            else -> throw ImageManipulatorError("unsupported output format $format")
        }
        val tw = if (spec.has("width")) spec.optInt("width") else img.width
        val th = if (spec.has("height")) spec.optInt("height") else img.height
        if (tw < 1 || th < 1 || tw > 16384 || th > 16384) throw ImageManipulatorError("bad output size ${tw}x$th")
        if (tw != img.width || th != img.height) {
            val out = scaled(img, tw, th)
            if (owned) img.recycle()
            img = out; owned = true
        }
        if (format == "jpeg" && img.hasAlpha()) {  // JPEG has no alpha: flatten onto white
            val flat = android.graphics.Bitmap.createBitmap(img.width, img.height, android.graphics.Bitmap.Config.ARGB_8888)
            val c = android.graphics.Canvas(flat)
            c.drawColor(android.graphics.Color.WHITE)
            c.drawBitmap(img, 0f, 0f, null)
            if (owned) img.recycle()
            img = flat; owned = true
        }
        val dir = java.io.File(cacheDir, "pyxpo-images").apply { mkdirs() }
        val file = java.io.File(dir, java.util.UUID.randomUUID().toString() + "." + (if (format == "jpeg") "jpg" else format))
        val q = (spec.optDouble("quality", 0.9).coerceIn(0.0, 1.0) * 100).toInt()
        try {
            file.outputStream().use { if (!img.compress(cf, q, it)) throw ImageManipulatorError("encoding failed") }
        } catch (e: java.io.IOException) {
            file.delete(); throw ImageManipulatorError("cannot write output: ${e.message}")
        }
        val result = mapOf("uri" to file.absolutePath, "fileSize" to file.length(), "width" to img.width, "height" to img.height,
            "format" to format, "mimeType" to mimeOf(format))
        if (owned) img.recycle()
        return result
    }

    fun close(h: Int) { handles.remove(h)?.bitmap?.recycle() }
}
