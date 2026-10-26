package org.jellyfin.androidtv.ui.shared.toolbar

import androidx.activity.compose.LocalActivity
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusGroup
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import java.util.UUID
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.focusRestorer
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.res.vectorResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.flow.filterNotNull
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.auth.repository.SessionRepository
import org.jellyfin.androidtv.auth.repository.UserRepository
import org.jellyfin.androidtv.data.repository.UserViewsRepository
import org.jellyfin.androidtv.ui.base.Icon
import org.jellyfin.androidtv.ui.base.Text
import org.jellyfin.androidtv.ui.navigation.ActivityDestinations
import org.jellyfin.androidtv.ui.navigation.Destinations
import org.jellyfin.androidtv.ui.navigation.NavigationRepository
import org.jellyfin.androidtv.ui.playback.MediaManager
import org.jellyfin.androidtv.ui.settings.compat.SettingsViewModel
import org.jellyfin.sdk.model.api.CollectionType
import org.koin.compose.koinInject
import org.koin.compose.viewmodel.koinActivityViewModel

private val RailAccent = Color(0xFF3D63E0)
private val RailInk = Color(0xFFF4F2EE)
private val RailClosed = 72.dp
private val RailOpen = 240.dp

/**
 * Everflix side menu. It stays narrow (icons only) and opens with labels when it receives focus,
 * like the Netflix TV app. Pressing right leaves it and goes back to the content.
 */
@Composable
fun MainRail(
	activeButton: MainToolbarActiveButton = MainToolbarActiveButton.None,
	modifier: Modifier = Modifier,
	activeLibrary: UUID? = null,
) {
	val navigationRepository = koinInject<NavigationRepository>()
	val mediaManager = koinInject<MediaManager>()
	val sessionRepository = koinInject<SessionRepository>()
	val userRepository = koinInject<UserRepository>()
	val settingsViewModel = koinActivityViewModel<SettingsViewModel>()
	val activity = LocalActivity.current
	val currentUser by remember { userRepository.currentUser.filterNotNull() }.collectAsState(null)
	val userViewsRepository = koinInject<UserViewsRepository>()
	val views by remember { userViewsRepository.views }.collectAsState(emptyList())

	var open by remember { mutableStateOf(false) }
	val width by animateDpAsState(if (open) RailOpen else RailClosed, label = "railWidth")
	val activeRequester = remember { FocusRequester() }

	Column(
		verticalArrangement = Arrangement.spacedBy(6.dp),
		modifier = modifier
			.width(width)
			.fillMaxHeight()
			.background(
				Brush.horizontalGradient(
					if (open) listOf(Color.Black, Color.Black.copy(alpha = 0.85f), Color.Transparent)
					else listOf(Color.Black.copy(alpha = 0.85f), Color.Transparent)
				)
			)
			.onFocusChanged { open = it.hasFocus }
			.focusRestorer(activeRequester)
			.focusGroup()
			.verticalScroll(rememberScrollState())
			.padding(horizontal = 12.dp, vertical = 28.dp),
	) {
		Text(
			text = if (open) "Everflix" else "E",
			color = RailAccent,
			fontSize = 26.sp,
			fontWeight = FontWeight.W800,
			modifier = Modifier.padding(start = 12.dp, bottom = 18.dp),
		)

		val focusTarget = if (activeLibrary != null) null else if (activeButton == MainToolbarActiveButton.None || activeButton == MainToolbarActiveButton.User) MainToolbarActiveButton.Home else activeButton
		fun target(button: MainToolbarActiveButton) = if (button == focusTarget) Modifier.focusRequester(activeRequester) else Modifier

		RailItem(
			icon = R.drawable.ic_search,
			label = stringResource(R.string.lbl_search),
			open = open,
			active = activeButton == MainToolbarActiveButton.Search,
			modifier = target(MainToolbarActiveButton.Search),
		) {
			if (activeButton != MainToolbarActiveButton.Search) navigationRepository.navigate(Destinations.search())
		}
		RailItem(
			icon = R.drawable.ic_house,
			label = stringResource(R.string.lbl_home),
			open = open,
			active = activeButton == MainToolbarActiveButton.Home,
			modifier = target(MainToolbarActiveButton.Home),
		) {
			if (activeButton != MainToolbarActiveButton.Home) navigationRepository.navigate(Destinations.home, replace = true)
		}
		RailItem(
			icon = R.drawable.ic_movie,
			label = "Películas",
			open = open,
			active = activeButton == MainToolbarActiveButton.Movies,
			modifier = target(MainToolbarActiveButton.Movies),
		) {
			if (activeButton != MainToolbarActiveButton.Movies) navigationRepository.navigate(Destinations.everflixAll(series = false))
		}
		RailItem(
			icon = R.drawable.ic_tv,
			label = "Series",
			open = open,
			active = activeButton == MainToolbarActiveButton.Series,
			modifier = target(MainToolbarActiveButton.Series),
		) {
			if (activeButton != MainToolbarActiveButton.Series) navigationRepository.navigate(Destinations.everflixAll(series = true))
		}

		Box(Modifier.padding(horizontal = 12.dp, vertical = 8.dp).fillMaxWidth().height(1.dp).background(RailInk.copy(alpha = 0.15f)))

		views.forEach { view ->
			val isActive = view.id == activeLibrary
			RailItem(
				icon = when (view.collectionType) {
					CollectionType.TVSHOWS -> R.drawable.ic_tv
					CollectionType.MOVIES -> R.drawable.ic_movie
					else -> R.drawable.ic_folder
				},
				label = view.name ?: "",
				open = open,
				active = isActive,
				modifier = if (isActive) Modifier.focusRequester(activeRequester) else Modifier,
			) {
				if (!isActive) navigationRepository.navigate(Destinations.everflixLibrary(view))
			}
		}

		Box(Modifier.padding(horizontal = 12.dp, vertical = 8.dp).fillMaxWidth().height(1.dp).background(RailInk.copy(alpha = 0.15f)))

		RailItem(
			icon = R.drawable.ic_settings,
			label = stringResource(R.string.lbl_settings),
			open = open,
			active = false,
		) { settingsViewModel.show() }

		RailItem(
			icon = R.drawable.ic_user,
			label = currentUser?.name ?: stringResource(R.string.lbl_switch_user),
			open = open,
			active = false,
		) {
			mediaManager.clearAudioQueue()
			sessionRepository.destroyCurrentSession()
			activity?.startActivity(ActivityDestinations.startup(activity))
			activity?.finishAfterTransition()
		}
	}
}

@Composable
private fun RailItem(
	icon: Int,
	label: String,
	open: Boolean,
	active: Boolean,
	modifier: Modifier = Modifier,
	onClick: () -> Unit,
) {
	val interaction = remember { MutableInteractionSource() }
	val focused by interaction.collectIsFocusedAsState()
	val content = when {
		focused -> Color.Black
		active -> RailInk
		else -> RailInk.copy(alpha = 0.6f)
	}

	Row(
		verticalAlignment = Alignment.CenterVertically,
		horizontalArrangement = Arrangement.spacedBy(16.dp),
		modifier = modifier
			.fillMaxWidth()
			.height(40.dp)
			.clip(RoundedCornerShape(8.dp))
			.background(if (focused) RailInk else Color.Transparent)
			.clickable(interactionSource = interaction, indication = null, onClick = onClick)
			.padding(horizontal = 12.dp),
	) {
		Icon(
			imageVector = ImageVector.vectorResource(icon),
			contentDescription = label,
			tint = content,
			modifier = Modifier.size(22.dp),
		)
		if (open) {
			Text(text = label, color = content, fontSize = 16.sp, fontWeight = FontWeight.W600, maxLines = 1)
			Spacer(Modifier.weight(1f))
		}
		if (active) Box(Modifier.size(6.dp).clip(CircleShape).background(RailAccent))
	}
}
