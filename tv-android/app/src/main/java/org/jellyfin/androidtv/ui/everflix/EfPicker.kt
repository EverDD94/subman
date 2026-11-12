package org.jellyfin.androidtv.ui.everflix

import android.app.Dialog
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.StateListDrawable
import android.util.TypedValue
import android.view.Gravity
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.res.ResourcesCompat
import org.jellyfin.androidtv.R
import java.util.Locale

/**
 * Everflix side panel that replaces the old Jellyfin popup menus in the player
 * (audio, subtitles, speed, quality, zoom). Same look as the Roku app.
 */
class EfPicker(
	private val context: Context,
	private val title: String,
	private val options: List<Option>,
	private val onPick: (Int) -> Unit,
	private val onDismiss: () -> Unit = {},
) {
	data class Option(val label: String, val checked: Boolean = false)

	private var dialog: Dialog? = null

	private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), context.resources.displayMetrics).toInt()

	fun show() {
		val bold = ResourcesCompat.getFont(context, R.font.archivo_extrabold)
		val semi = ResourcesCompat.getFont(context, R.font.archivo_semibold)
		val panel = LinearLayout(context).apply {
			orientation = LinearLayout.VERTICAL
			setBackgroundColor(Color.parseColor("#F512151C"))
			setPadding(dp(28), dp(44), dp(28), dp(28))
		}
		panel.addView(TextView(context).apply {
			text = title
			typeface = bold
			setTextColor(Color.parseColor("#F4F2EE"))
			setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
			setPadding(dp(12), 0, 0, dp(18))
		})
		val list = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
		var focusFirst: TextView? = null
		options.forEachIndexed { i, opt ->
			val row = TextView(context).apply {
				text = opt.label
				typeface = semi
				isSingleLine = true
				setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
				setPadding(dp(16), dp(12), dp(16), dp(12))
				isFocusable = true
				isFocusableInTouchMode = true
				background = StateListDrawable().apply {
					addState(intArrayOf(android.R.attr.state_focused), GradientDrawable().apply {
						setColor(Color.WHITE); cornerRadius = dp(10).toFloat()
					})
					addState(intArrayOf(), ColorDrawable(Color.TRANSPARENT))
				}
				val normal = if (opt.checked) Color.parseColor("#F4F2EE") else Color.parseColor("#B3F4F2EE")
				setTextColor(normal)
				val check = if (opt.checked) ResourcesCompat.getDrawable(context.resources, R.drawable.ic_check, context.theme)?.mutate() else null
				check?.setBounds(0, 0, dp(18), dp(18))
				check?.setTint(Color.parseColor("#9DB4FF"))
				setCompoundDrawables(null, null, check, null)
				setOnFocusChangeListener { _, has ->
					setTextColor(if (has) Color.BLACK else normal)
					check?.setTint(if (has) Color.BLACK else Color.parseColor("#9DB4FF"))
				}
				setOnClickListener {
					dismiss()
					onPick(i)
				}
			}
			list.addView(row, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(4) })
			if (focusFirst == null || opt.checked) focusFirst = row
		}
		panel.addView(ScrollView(context).apply { addView(list); isVerticalScrollBarEnabled = false })

		dialog = Dialog(context, android.R.style.Theme_Translucent_NoTitleBar).apply {
			setContentView(panel)
			window?.apply {
				setGravity(Gravity.END)
				setLayout(dp(360), ViewGroup.LayoutParams.MATCH_PARENT)
				addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND)
				setDimAmount(0.55f)
			}
			setOnDismissListener { dialog = null; onDismiss() }
			show()
		}
		focusFirst?.requestFocus()
	}

	fun dismiss() {
		dialog?.dismiss()
	}

	companion object {
		private val NAMES = mapOf(
			"spa" to "Español", "es" to "Español", "esp" to "Español",
			"eng" to "Inglés", "en" to "Inglés", "por" to "Portugués", "pt" to "Portugués",
			"fre" to "Francés", "fra" to "Francés", "fr" to "Francés", "jpn" to "Japonés", "ja" to "Japonés",
			"ita" to "Italiano", "it" to "Italiano", "ger" to "Alemán", "deu" to "Alemán", "de" to "Alemán",
			"chi" to "Chino", "zho" to "Chino", "kor" to "Coreano", "cat" to "Catalán",
			"mul" to "Varios", "zxx" to "Sin idioma",
		)

		/** Language code (ISO 639-1/2) to its Spanish name; "" when unknown. */
		fun langName(code: String?): String {
			if (code.isNullOrBlank() || code.equals("und", true)) return ""
			NAMES[code.lowercase()]?.let { return it }
			val n = Locale.forLanguageTag(code).getDisplayLanguage(Locale("es"))
			return if (n.isBlank() || n.equals(code, true)) code.uppercase() else n.replaceFirstChar { it.uppercase() }
		}

		/** Adds " · 5.1" or " (2)" to repeated names so they can be told apart. */
		fun disambiguate(names: List<String>, extras: List<String>): List<String> {
			val counts = names.groupingBy { it }.eachCount()
			val seen = HashMap<String, Int>()
			return names.mapIndexed { i, n ->
				if ((counts[n] ?: 0) < 2) n
				else {
					val k = (seen[n] ?: 0) + 1; seen[n] = k
					if (extras[i].isNotBlank()) "$n · ${extras[i]}" else "$n ($k)"
				}
			}.let { list ->
				// still repeated (same channels): number them
				val c2 = list.groupingBy { it }.eachCount(); val s2 = HashMap<String, Int>()
				list.map { n -> if ((c2[n] ?: 0) < 2) n else { val k = (s2[n] ?: 0) + 1; s2[n] = k; "$n ($k)" } }
			}
		}

		fun channels(ch: Int?): String = when (ch) { 1 -> "Mono"; 2 -> "Estéreo"; 6 -> "5.1"; 8 -> "7.1"; else -> "" }
	}
}
