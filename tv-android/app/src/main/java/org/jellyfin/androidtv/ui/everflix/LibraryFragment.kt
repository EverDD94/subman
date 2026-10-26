package org.jellyfin.androidtv.ui.everflix

import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.animateFloatAsState
import android.widget.ImageView
import org.jellyfin.androidtv.ui.composable.AsyncImage
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.fragment.app.Fragment
import androidx.fragment.compose.content
import coil3.compose.rememberAsyncImagePainter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import org.jellyfin.androidtv.constant.Extras
import org.jellyfin.androidtv.data.repository.ItemRepository
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.home.HeroDetails
import org.jellyfin.androidtv.ui.navigation.Destinations
import org.jellyfin.androidtv.ui.navigation.NavigationRepository
import org.jellyfin.androidtv.ui.shared.toolbar.MainRail
import org.jellyfin.androidtv.ui.shared.toolbar.MainToolbarActiveButton
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.imageApi
import org.jellyfin.sdk.api.client.extensions.itemsApi
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.model.api.BaseItemKind
import org.jellyfin.sdk.model.api.CollectionType
import org.jellyfin.sdk.model.api.ImageType
import org.jellyfin.sdk.model.api.ItemSortBy
import org.jellyfin.sdk.model.api.SortOrder
import org.koin.compose.koinInject
import timber.log.Timber

/**
 * Everflix library screen (Series / Películas), split in two: the focused title fills the top
 * with its images cycling like a preview, and the whole library is one row underneath.
 */
const val EXTRA_ALL = "everflix_all"
const val ALL_SERIES = "series"
const val ALL_MOVIES = "movies"

class LibraryFragment : Fragment() {
	override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?) = content {
		val library = remember {
			requireArguments().getString(Extras.Folder)?.let { Json.decodeFromString(BaseItemDto.serializer(), it) }
		}
		val allShows = remember { requireArguments().getString(EXTRA_ALL) == ALL_SERIES }
		LibraryScreen(library, allShows)
	}
}

@Composable
private fun LibraryScreen(library: BaseItemDto?, allShows: Boolean) {
	val api = koinInject<ApiClient>()
	val navigationRepository = koinInject<NavigationRepository>()
	// Without a library this is the whole catalogue of one kind (the "Películas" and "Series" menu entries)
	val kinds = when (library?.collectionType) {
		null -> setOf(if (allShows) BaseItemKind.SERIES else BaseItemKind.MOVIE)
		CollectionType.TVSHOWS -> setOf(BaseItemKind.SERIES)
		CollectionType.MOVIES -> setOf(BaseItemKind.MOVIE)
		CollectionType.BOXSETS -> setOf(BaseItemKind.BOX_SET)
		else -> setOf(BaseItemKind.SERIES, BaseItemKind.MOVIE, BaseItemKind.VIDEO)
	}
	val title = library?.name ?: if (allShows) "Series" else "Películas"

	var items by remember { mutableStateOf<List<BaseItemDto>>(emptyList()) }
	var focused by remember { mutableIntStateOf(0) }
	val firstCard = remember { FocusRequester() }

	LaunchedEffect(library?.id, allShows) {
		items = withContext(Dispatchers.IO) {
			runCatching {
				api.itemsApi.getItems(
					parentId = library?.id,
					recursive = true,
					includeItemTypes = kinds,
					sortBy = setOf(ItemSortBy.DATE_CREATED),
					sortOrder = setOf(SortOrder.DESCENDING),
					fields = ItemRepository.itemFields,
					limit = if (library == null) 600 else 300,
				).content.items
			}.onFailure { Timber.w(it, "Failed to load library") }.getOrDefault(emptyList())
		}
		if (items.isNotEmpty()) runCatching { firstCard.requestFocus() }
	}

	val current = items.getOrNull(focused)

	Box(Modifier.fillMaxSize().background(Color(0xFF0B0D12))) {
		if (current != null) PreviewBackdrop(api, current)

		Row(Modifier.fillMaxSize()) {
			MainRail(
				when {
					library != null -> MainToolbarActiveButton.None
					allShows -> MainToolbarActiveButton.Series
					else -> MainToolbarActiveButton.Movies
				},
				activeLibrary = library?.id,
			)

			Column(Modifier.fillMaxSize()) {
				Box(Modifier.fillMaxWidth().height(330.dp).padding(start = 24.dp, top = 36.dp)) {
					if (current != null) HeroDetails(current, preview = true)
				}

				Text(
					text = "$title · ${items.size}".uppercase(),
					color = Color.White.copy(alpha = 0.8f),
					fontSize = 13.sp,
					fontWeight = FontWeight.W800,
					letterSpacing = 1.sp,
					modifier = Modifier.padding(start = 24.dp, bottom = 10.dp),
				)

				LazyRow(
					horizontalArrangement = Arrangement.spacedBy(12.dp),
					contentPadding = PaddingValues(start = 24.dp, end = 48.dp, top = 8.dp, bottom = 24.dp),
				) {
					itemsIndexed(items, key = { _, it -> it.id }) { index, item ->
						LibraryCard(
							api = api,
							item = item,
							modifier = if (index == 0) Modifier.focusRequester(firstCard) else Modifier,
							onFocus = { focused = index },
							onClick = { navigationRepository.navigate(Destinations.itemDetails(item.id)) },
						)
					}
				}
			}
		}
	}
}

/** Full-width image of the focused title, slowly cycling through its backdrops like a silent preview. */
@Composable
internal fun PreviewBackdrop(api: ApiClient, item: BaseItemDto) {
	val count = item.backdropImageTags?.size ?: 0
	var index by remember(item.id) { mutableIntStateOf(0) }
	LaunchedEffect(item.id, count) {
		while (count > 1) {
			delay(3500)
			index = (index + 1) % count
		}
	}
	val url = if (count > 0) api.imageApi.getItemImageUrl(itemId = item.id, imageType = ImageType.BACKDROP, imageIndex = index, maxWidth = 1920)
	else api.imageApi.getItemImageUrl(itemId = item.id, imageType = ImageType.PRIMARY, maxWidth = 1280)
	val zoom by animateFloatAsState(if (index % 2 == 0) 1f else 1.06f, label = "previewZoom")

	Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f)) {
		Crossfade(targetState = url, label = "preview") { target ->
			AsyncImage(
				url = target,
				scaleType = ImageView.ScaleType.CENTER_CROP,
				modifier = Modifier.fillMaxSize().scale(zoom),
			)
		}
		Box(
			Modifier.fillMaxSize().background(
				Brush.horizontalGradient(0f to Color(0xFF0B0D12), 0.35f to Color(0xCC0B0D12), 0.7f to Color.Transparent)
			)
		)
		Box(
			Modifier.fillMaxSize().background(
				Brush.verticalGradient(0.35f to Color.Transparent, 0.62f to Color(0xFF0B0D12))
			)
		)
	}
}

@Composable
private fun LibraryCard(
	api: ApiClient,
	item: BaseItemDto,
	modifier: Modifier = Modifier,
	onFocus: () -> Unit,
	onClick: () -> Unit,
) {
	val interaction = remember { MutableInteractionSource() }
	val isFocused by interaction.collectIsFocusedAsState()
	val scale by animateFloatAsState(if (isFocused) 1.08f else 1f, label = "cardScale")
	val hasBackdrop = !item.backdropImageTags.isNullOrEmpty()
	val url = if (hasBackdrop) api.imageApi.getItemImageUrl(itemId = item.id, imageType = ImageType.BACKDROP, maxWidth = 480)
	else api.imageApi.getItemImageUrl(itemId = item.id, imageType = ImageType.PRIMARY, maxWidth = 480)

	Box(
		modifier = modifier
			.width(220.dp)
			.aspectRatio(16f / 9f)
			.scale(scale)
			.clip(RoundedCornerShape(8.dp))
			.then(if (isFocused) Modifier.border(3.dp, Color.White, RoundedCornerShape(8.dp)) else Modifier)
			.background(Color(0xFF161A22))
			.onFocusChanged { if (it.isFocused) onFocus() }
			.clickable(interactionSource = interaction, indication = null, onClick = onClick),
	) {
		AsyncImage(
			url = url,
			scaleType = ImageView.ScaleType.CENTER_CROP,
			modifier = Modifier.fillMaxSize(),
		)
		Box(Modifier.fillMaxSize().background(Brush.verticalGradient(0.5f to Color.Transparent, 1f to Color(0xCC000000))))
		Text(
			text = (item.name ?: "").uppercase(),
			color = Color.White,
			fontSize = 13.sp,
			fontWeight = FontWeight.W800,
			maxLines = 1,
			overflow = TextOverflow.Ellipsis,
			modifier = Modifier.align(Alignment.BottomStart).padding(10.dp),
		)
	}
}
