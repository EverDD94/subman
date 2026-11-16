package org.jellyfin.androidtv.ui.everflix

import android.content.Context
import android.graphics.drawable.GradientDrawable
import android.util.TypedValue
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.res.ResourcesCompat
import org.jellyfin.androidtv.R

/**
 * Everflix: el cartel de arriba con los mensajes que mandas desde Videotecario o Jellyfin
 * (igual que en la web y en Roku), en vez del aviso pequeño de Android.
 */
object EverflixNotice {
	@Suppress("DEPRECATION")
	fun show(context: Context, header: String?, text: String?) {
		val density = context.resources.displayMetrics.density
		fun dp(value: Int) = (value * density).toInt()

		val row = LinearLayout(context).apply {
			orientation = LinearLayout.HORIZONTAL
			gravity = Gravity.CENTER_VERTICAL
			setPadding(dp(28), dp(16), dp(28), dp(16))
			background = GradientDrawable().apply {
				cornerRadius = dp(18).toFloat()
				setColor(0xEE12151C.toInt())
			}
			elevation = dp(8).toFloat()
		}
		val column = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
		// sin logo; si no hay nombre (anónimo) sale sólo el mensaje
		if (!header.isNullOrBlank()) column.addView(TextView(context).apply {
			this.text = header
			setTextColor(0xFFF4F2EE.toInt())
			setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
			typeface = ResourcesCompat.getFont(context, R.font.archivo_extrabold)
		})
		column.addView(TextView(context).apply {
			this.text = text.orEmpty()
			setTextColor(0xFFD6DAE3.toInt())
			setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
			typeface = ResourcesCompat.getFont(context, R.font.archivo_regular)
			maxWidth = dp(520)
			maxLines = 3
		})
		row.addView(column)

		Toast(context).apply {
			duration = Toast.LENGTH_LONG
			setGravity(Gravity.TOP or Gravity.CENTER_HORIZONTAL, 0, dp(28))
			view = row
		}.show()
	}
}
