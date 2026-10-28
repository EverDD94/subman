package org.jellyfin.androidtv.ui.everflix

import android.content.Context
import android.view.ViewGroup
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.platform.findViewTreeCompositionContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.leanback.widget.ArrayObjectAdapter
import androidx.leanback.widget.HeaderItem
import androidx.leanback.widget.ListRow
import androidx.leanback.widget.Presenter
import androidx.leanback.widget.Row
import androidx.lifecycle.findViewTreeLifecycleOwner
import androidx.lifecycle.setViewTreeLifecycleOwner
import androidx.savedstate.findViewTreeSavedStateRegistryOwner
import androidx.savedstate.setViewTreeSavedStateRegistryOwner
import kotlinx.coroutines.flow.MutableStateFlow
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.composable.AsyncImage
import org.jellyfin.androidtv.ui.home.HomeFragmentRow
import org.jellyfin.androidtv.ui.presentation.CardPresenter
import org.jellyfin.androidtv.ui.presentation.MutableObjectAdapter

/** What was asked for from the home screen, shared by every discover row so the cards update at once. */
object DiscoverAsked {
	val keys = MutableStateFlow(setOf<String>())
	fun key(title: DiscoverTitle) = "${title.kind}${title.tmdb}"
}

/** A home screen row of titles from outside the library (releases, streaming platforms). */
class HomeFragmentDiscoverRow(private val row: DiscoverRow, private val admin: Boolean) : HomeFragmentRow {
	override fun addToRowsAdapter(context: Context, cardPresenter: CardPresenter, rowsAdapter: MutableObjectAdapter<Row>) {
		val adapter = ArrayObjectAdapter(DiscoverCardPresenter(admin))
		row.titles.forEach { adapter.add(it) }
		DiscoverAsked.keys.value += row.titles.filter { it.asked }.map(DiscoverAsked::key)
		rowsAdapter.add(ListRow(HeaderItem(row.title), adapter))
	}
}

private class DiscoverCardPresenter(private val admin: Boolean) : Presenter() {
	override fun onCreateViewHolder(parent: ViewGroup): ViewHolder {
		val view = ComposeView(parent.context).apply {
			setParentCompositionContext(parent.findViewTreeCompositionContext())
			setViewTreeLifecycleOwner(parent.findViewTreeLifecycleOwner())
			setViewTreeSavedStateRegistryOwner(parent.findViewTreeSavedStateRegistryOwner())
			isFocusable = true
			isFocusableInTouchMode = true
		}
		return Holder(view)
	}

	override fun onBindViewHolder(viewHolder: ViewHolder, item: Any?) {
		(viewHolder as Holder).item.value = item as? DiscoverTitle
	}

	override fun onUnbindViewHolder(viewHolder: ViewHolder) {
		(viewHolder as Holder).item.value = null
	}

	private inner class Holder(view: ComposeView) : ViewHolder(view) {
		val item = MutableStateFlow<DiscoverTitle?>(null)
		private val focused = MutableStateFlow(false)

		init {
			view.onFocusChangeListener = { _, f -> focused.value = f }
			view.setContent {
				val title by item.collectAsState()
				val isFocused by focused.collectAsState()
				val asked by DiscoverAsked.keys.collectAsState()
				val t = title ?: return@setContent
				val shape = RoundedCornerShape(8.dp)
				Box(
					Modifier
						.size(100.dp, 150.dp)
						.clip(shape)
						.then(if (isFocused) Modifier.border(3.dp, Color.White, shape) else Modifier)
						.background(Color(0xFF161A22)),
				) {
					if (t.poster.isNotEmpty()) AsyncImage(url = t.poster, aspectRatio = 2f / 3f, modifier = Modifier.fillMaxSize())
					Box(Modifier.fillMaxSize().background(Brush.verticalGradient(0.45f to Color.Transparent, 1f to Color(0xE6000000))))
					Column(Modifier.align(Alignment.BottomStart).padding(8.dp)) {
						Text(t.title, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.W600, maxLines = 2, overflow = TextOverflow.Ellipsis)
						val done = DiscoverAsked.key(t) in asked
						Text(
							text = when {
								t.have -> "✓ En tu biblioteca"
								done -> if (admin) "✓ En Descargas" else "✓ Pedido"
								isFocused -> if (admin) "＋ Descargar" else "＋ Pedir"
								else -> t.year?.toString() ?: ""
							},
							color = if (t.have || done || isFocused) Color(0xFF9DB4FF) else Color.White.copy(alpha = 0.7f),
							fontSize = 11.sp,
							fontWeight = FontWeight.W800,
						)
					}
				}
			}
		}
	}
}
